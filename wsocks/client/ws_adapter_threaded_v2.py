"""
ThreadedCurlWebSocketV2 - 基于 curl_cffi 高层 WebSocket API 的实现

与 V1 的区别：
- 使用 ws.recv() / ws.send() 高层方法，不操作原始 CFFI/socket
- send/recv 各用独立线程，互不阻塞
- 不解析业务协议，适配器只负责 WebSocket 帧
- 无版本强依赖

注意：send/recv 线程共享同一个 curl easy handle。libcurl 官方不保证
同一 handle 的跨线程安全，但 curl_ws_recv/curl_ws_send 操作不同方向，
实践中未观察到问题。如遇稳定性问题，可考虑回退到 V1 的单线程 select 模型。
"""
import asyncio
import threading
import queue
import time
from typing import Optional, AsyncIterator

from wsocks.client.ws_adapter import WebSocketAdapter
from wsocks.common.logger import setup_logger

logger = setup_logger()


class ThreadedCurlWebSocketV2(WebSocketAdapter):
    def __init__(self, impersonate: str = "chrome124"):
        self._impersonate = impersonate
        self._url = None
        self._proxy = None
        self._read_timeout = None

        self._ws = None
        self._session = None

        self._send_queue: queue.Queue = queue.Queue(maxsize=4096)
        self._recv_queue: queue.Queue = queue.Queue(maxsize=512)

        self._running = False
        self._connected_event = threading.Event()
        self._connect_error: Optional[Exception] = None

        self._send_thread: Optional[threading.Thread] = None
        self._recv_thread: Optional[threading.Thread] = None

        self._closed_conn_ids: set = set()
        self._cleanup_lock = threading.Lock()
        self._cleaned_up = False

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    async def connect(self, url: str, **kwargs) -> 'ThreadedCurlWebSocketV2':
        try:
            import curl_cffi  # noqa: F401
        except ImportError as e:
            raise ImportError("curl_cffi not installed. pip install curl_cffi") from e

        self._url = url
        self._proxy = kwargs.get('proxy')
        self._read_timeout = kwargs.get('read_timeout')
        self._running = True

        # 在线程里完成阻塞的握手，主线程等待结果
        connect_thread = threading.Thread(target=self._do_connect, daemon=True)
        connect_thread.start()

        loop = asyncio.get_event_loop()
        success = await loop.run_in_executor(None, self._connected_event.wait, 15)

        if self._connect_error:
            raise self._connect_error
        if not success:
            self._running = False
            raise TimeoutError("WebSocket connection timeout (15s)")

        logger.info(f"[CurlWSv2] Connected to {url} (impersonate={self._impersonate})")
        return self

    async def send(self, data: bytes, priority: bool = False, conn_id: Optional[bytes] = None) -> None:
        if not self._running:
            raise RuntimeError("WebSocket is closed")
        if conn_id and conn_id in self._closed_conn_ids:
            return
        try:
            self._send_queue.put_nowait((data, conn_id))
        except queue.Full:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._send_queue.put, (data, conn_id))

    async def recv(self) -> bytes:
        if not self._running and self._recv_queue.empty():
            raise RuntimeError("WebSocket is closed")
        loop = asyncio.get_event_loop()
        while True:
            try:
                item = await loop.run_in_executor(
                    None, self._recv_queue.get, True, 1.0
                )
            except queue.Empty:
                if not self._running and self._recv_queue.empty():
                    raise RuntimeError("WebSocket is closed")
                continue
            if isinstance(item, Exception):
                raise item
            return item

    async def mark_connection_closed(self, conn_id: bytes) -> None:
        self._closed_conn_ids.add(conn_id)

    async def close(self) -> None:
        self._running = False
        # 往队列里塞 sentinel，让 send 线程能退出
        try:
            self._send_queue.put_nowait(None)
        except queue.Full:
            pass
        if self._send_thread:
            self._send_thread.join(timeout=2)
        if self._recv_thread:
            self._recv_thread.join(timeout=2)
        self._cleanup()

    def __aiter__(self) -> AsyncIterator:
        return self

    async def __anext__(self):
        try:
            return await self.recv()
        except Exception:
            raise StopAsyncIteration

    # ------------------------------------------------------------------ #
    #  Internal: connect                                                   #
    # ------------------------------------------------------------------ #

    def _do_connect(self):
        """在独立线程里完成握手，成功后启动 send/recv 线程"""
        try:
            from curl_cffi.requests import Session

            session_kwargs = dict(impersonate=self._impersonate)
            self._session = Session(**session_kwargs)

            connect_kwargs = {}
            if self._proxy:
                connect_kwargs['proxy'] = self._proxy
                logger.info(f"[CurlWSv2] Using proxy: {self._proxy}")

            self._ws = self._session.ws_connect(self._url, **connect_kwargs)

            # 握手成功，启动工作线程
            self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
            self._send_thread = threading.Thread(target=self._send_loop, daemon=True)
            self._recv_thread.start()
            self._send_thread.start()

            self._connected_event.set()

        except Exception as e:
            logger.error(f"[CurlWSv2] Connect error: {e}")
            self._connect_error = e
            self._running = False
            self._connected_event.set()

    # ------------------------------------------------------------------ #
    #  Internal: recv loop                                                 #
    # ------------------------------------------------------------------ #

    def _recv_with_timeout(self) -> tuple:
        """带超时的 recv，替代 ws.recv() 的无限阻塞循环。

        ws.recv() 在无数据时会死循环 select(0.5s) → AGAIN → select(0.5s)，
        永不返回，导致看门狗失效。这里手动实现，每轮 select 后检查 _running
        和 read_timeout，确保休眠唤醒后能及时检测到死连接。
        """
        from curl_cffi.const import CurlWsFlag, CurlECode, CurlInfo
        # from curl_cffi._wrapper import CurlError
        from select import select as _select
        from curl_cffi import CurlError

        chunks = []
        flags = 0

        sock_fd = self._ws.curl.getinfo(CurlInfo.ACTIVESOCKET)

        while self._running:
            try:
                chunk, frame = self._ws.recv_fragment()
                flags = frame.flags
                chunks.append(chunk)
                if frame.bytesleft == 0 and flags & CurlWsFlag.CONT == 0:
                    break
            except CurlError as e:
                if e.code == CurlECode.AGAIN:
                    # 每次 select 0.5s，然后回到 while 检查 _running
                    _select([sock_fd], [], [], 0.5)
                else:
                    raise

        if not self._running:
            raise ConnectionError("WebSocket shutting down")

        return b"".join(chunks), flags

    def _recv_loop(self):
        """持续从 WebSocket 接收数据，推入 recv_queue

        使用 _recv_with_timeout 替代 ws.recv()，每 0.5s 检查一次
        _running 和 read_timeout，确保休眠唤醒后能及时触发重连。
        """
        from curl_cffi.const import CurlWsFlag

        last_recv = time.time()

        try:
            while self._running:
                # read_timeout 看门狗：_recv_with_timeout 每 0.5s 让出一次控制权
                if self._read_timeout and (time.time() - last_recv > self._read_timeout):
                    raise TimeoutError(f"[CurlWSv2] Read timeout ({self._read_timeout}s)")

                try:
                    data, flags = self._recv_with_timeout()
                except ConnectionError:
                    break  # shutting down
                except Exception as e:
                    if self._running:
                        logger.info(f"[CurlWSv2] recv error: {e}")
                    break

                last_recv = time.time()

                if flags & CurlWsFlag.CLOSE:
                    logger.info("[CurlWSv2] Received CLOSE frame")
                    break

                if data:
                    try:
                        self._recv_queue.put(data, timeout=5.0)
                    except queue.Full:
                        logger.warning("[CurlWSv2] recv_queue full for 5s, dropping message")

        except Exception as e:
            logger.error(f"[CurlWSv2] recv loop error: {e}")
        finally:
            self._running = False
            try:
                self._recv_queue.put(ConnectionError("WebSocket closed"), timeout=0.1)
            except Exception:
                pass
            logger.debug("[CurlWSv2] recv loop exited")
            self._cleanup()

    # ------------------------------------------------------------------ #
    #  Internal: send loop                                                 #
    # ------------------------------------------------------------------ #

    def _send_loop(self):
        """从 send_queue 取数据，调用 ws.send() 发出"""
        from curl_cffi.const import CurlWsFlag

        try:
            while self._running:
                try:
                    item = self._send_queue.get(timeout=0.5)
                except queue.Empty:
                    continue

                if item is None:  # sentinel，退出信号
                    break

                data, conn_id = item

                if conn_id and conn_id in self._closed_conn_ids:
                    continue

                try:
                    self._ws.send(data, CurlWsFlag.BINARY)
                except Exception as e:
                    if self._running:
                        logger.error(f"[CurlWSv2] send error: {e}")
                    break

        except Exception as e:
            logger.error(f"[CurlWSv2] send loop error: {e}")
        finally:
            self._running = False
            logger.debug("[CurlWSv2] send loop exited")

    # ------------------------------------------------------------------ #
    #  Internal: cleanup                                                   #
    # ------------------------------------------------------------------ #

    def _cleanup(self):
        with self._cleanup_lock:
            if self._cleaned_up:
                return
            self._cleaned_up = True

        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._session:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None
