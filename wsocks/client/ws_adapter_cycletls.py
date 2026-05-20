"""
WebSocket 适配器 - CycleTLS 版本 (V3)

使用 CycleTLS 的 WebSocketConnection 实现 TLS 指纹伪装。
CycleTLS 基于 Go utls 库，通过 JA3 字符串控制 TLS 握手指纹。

特点：
- send/recv 各用独立线程，通过 queue 与 asyncio 桥接
- CycleTLS 的阻塞 API (Go FFI) 天然线程安全，无 curl handle 共享问题
- 不解析业务协议，适配器只负责 WebSocket 帧收发
"""
import asyncio
import threading
import queue
import time
from typing import Optional, AsyncIterator

from wsocks.client.ws_adapter import WebSocketAdapter
from wsocks.common.logger import setup_logger

logger = setup_logger()

# Safari 17.5 的 JA3 指纹
JA3_SAFARI = (
    "771,4865-4866-4867-49196-49195-52393-49200-49199-52392-49162-49161"
    "-49172-49171-157-156-53-47-49160-49170-10,"
    "0-23-65281-10-11-16-5-13-18-51-45-43-27-21,"
    "29-23-24-25,"
    "0"
)

# Chrome 124 的 JA3 指纹
JA3_CHROME = (
    "771,4865-4866-4867-49195-49199-49196-49200-52393-52392-49171-49172"
    "-156-157-47-53,"
    "0-23-65281-10-11-35-16-5-13-18-51-45-43-27-17513-21,"
    "29-23-24,"
    "0"
)

# 指纹映射表
JA3_MAP = {
    "safari": JA3_SAFARI,
    "chrome": JA3_CHROME,
    "chrome124": JA3_CHROME,
}


class CycleTLSWebSocket(WebSocketAdapter):
    def __init__(self, ja3: Optional[str] = None, impersonate: Optional[str] = None):
        """
        Args:
            ja3: 直接指定 JA3 字符串
            impersonate: 浏览器名称 (safari/chrome)，会映射为对应的 JA3
        """
        if ja3:
            self._ja3 = ja3
        elif impersonate:
            key = impersonate.lower().rstrip("0123456789")
            self._ja3 = JA3_MAP.get(key, JA3_MAP.get(impersonate, JA3_CHROME))
        else:
            self._ja3 = JA3_CHROME

        self._impersonate_label = impersonate or "custom"
        self._url = None
        self._proxy = None
        self._read_timeout = None

        self._ws = None  # CycleTLS WebSocketConnection

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

    async def connect(self, url: str, **kwargs) -> 'CycleTLSWebSocket':
        try:
            from cycletls.websocket import WebSocketConnection  # noqa: F401
        except ImportError as e:
            raise ImportError("cycletls not installed. pip install cycletls") from e

        self._url = url
        self._proxy = kwargs.get('proxy')
        self._read_timeout = kwargs.get('read_timeout')
        self._running = True

        connect_thread = threading.Thread(target=self._do_connect, daemon=True)
        connect_thread.start()

        loop = asyncio.get_event_loop()
        success = await loop.run_in_executor(None, self._connected_event.wait, 15)

        if self._connect_error:
            raise self._connect_error
        if not success:
            self._running = False
            raise TimeoutError("WebSocket connection timeout (15s)")

        logger.info(f"[CycleTLS] Connected to {url} (ja3={self._impersonate_label})")
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
        try:
            from cycletls.websocket import WebSocketConnection

            ws_kwargs = dict(
                url=self._url,
                ja3=self._ja3,
                timeout=10,
            )
            if self._proxy:
                ws_kwargs['proxy'] = self._proxy
                logger.info(f"[CycleTLS] Using proxy: {self._proxy}")

            self._ws = WebSocketConnection(**ws_kwargs)
            self._ws.connect()

            self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
            self._send_thread = threading.Thread(target=self._send_loop, daemon=True)
            self._recv_thread.start()
            self._send_thread.start()

            self._connected_event.set()

        except Exception as e:
            logger.error(f"[CycleTLS] Connect error: {e}")
            self._connect_error = e
            self._running = False
            self._connected_event.set()

    # ------------------------------------------------------------------ #
    #  Internal: recv loop                                                 #
    # ------------------------------------------------------------------ #

    def _recv_loop(self):
        from cycletls.websocket import MessageType

        last_recv = time.time()

        try:
            while self._running:
                if self._read_timeout and (time.time() - last_recv > self._read_timeout):
                    raise TimeoutError(f"[CycleTLS] Read timeout ({self._read_timeout}s)")

                try:
                    msg = self._ws.receive()
                except Exception as e:
                    if self._running:
                        logger.info(f"[CycleTLS] recv error: {e}")
                    break

                last_recv = time.time()

                if msg.type == MessageType.CLOSE:
                    logger.info("[CycleTLS] Received CLOSE frame")
                    break

                if msg.type in (MessageType.PING, MessageType.PONG):
                    continue

                data = msg.data
                if isinstance(data, str):
                    data = data.encode()

                if data:
                    try:
                        self._recv_queue.put(data, timeout=5.0)
                    except queue.Full:
                        logger.warning("[CycleTLS] recv_queue full for 5s, dropping message")

        except Exception as e:
            logger.error(f"[CycleTLS] recv loop error: {e}")
        finally:
            self._running = False
            try:
                self._recv_queue.put(ConnectionError("WebSocket closed"), timeout=0.1)
            except Exception:
                pass
            logger.debug("[CycleTLS] recv loop exited")
            self._cleanup()

    # ------------------------------------------------------------------ #
    #  Internal: send loop                                                 #
    # ------------------------------------------------------------------ #

    def _send_loop(self):
        try:
            while self._running:
                try:
                    item = self._send_queue.get(timeout=0.5)
                except queue.Empty:
                    continue

                if item is None:
                    break

                data, conn_id = item

                if conn_id and conn_id in self._closed_conn_ids:
                    continue

                try:
                    self._ws.send(data, binary=True)
                except Exception as e:
                    if self._running:
                        logger.error(f"[CycleTLS] send error: {e}")
                    break

        except Exception as e:
            logger.error(f"[CycleTLS] send loop error: {e}")
        finally:
            self._running = False
            logger.debug("[CycleTLS] send loop exited")

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
