"""
WebSocket 适配器 - 单线程 IO 复用版本 (最终防泄露版)
1. 增加连接超时 (CONNECTTIMEOUT)，防止线程泄露
2. 启用 TCP Keep-Alive，防止静默断开
3. 增加 Read Watchdog，主动查杀僵尸连接
"""
import asyncio
import threading
import queue
import logging
import time
import select
import struct
from typing import Optional, AsyncIterator

from wsocks.client.ws_adapter import WebSocketAdapter
from wsocks.common.logger import setup_logger

logger = setup_logger()

REQUIRED_CURL_CFFI_VERSION = '0.15.0'


class ConnectionClosed(Exception):
    """自定义异常：连接被服务端关闭"""
    pass


class ThreadedCurlWebSocket(WebSocketAdapter):
    def __init__(self, impersonate: str = "chrome124"):
        self._impersonate = impersonate
        self._url = None
        self._send_queue = queue.Queue(maxsize=4096)
        self._recv_queue = queue.Queue(maxsize=512)
        self._io_thread = None
        self._running = False
        self._connected_event = threading.Event()
        self._connect_error = None
        self._ws = None
        self._curl_handle = None
        self._sock_fd = -1
        self._closed_conn_ids = set()
        self._cffi_objects = {}
        self._recv_buffer = bytearray()
        self._PROTO_HEADER_SIZE = 19
        self._last_recv_time = 0.0
        self._read_timeout = None

    async def connect(self, url: str, **kwargs) -> 'ThreadedCurlWebSocket':
        try:
            import curl_cffi as _curl_cffi
            if getattr(_curl_cffi, "__version__", "unknown") != REQUIRED_CURL_CFFI_VERSION:
                raise ImportError(f"Unsupported curl_cffi version")
        except ImportError as e:
            raise ImportError("curl_cffi not installed") from e

        self._url = url
        self._running = True
        read_timeout: int = kwargs.get('read_timeout')
        self._read_timeout = read_timeout
        self._proxy = kwargs.get('proxy')

        self._io_thread = threading.Thread(target=self._io_worker, daemon=True)
        self._io_thread.start()

        # 等待连接，如果超时，说明底层线程可能卡住了
        loop = asyncio.get_event_loop()
        success = await loop.run_in_executor(None, self._connected_event.wait, 15)

        if self._connect_error:
            raise self._connect_error

        if not success:
            self._running = False
            # 注意：这里我们无法强制杀死线程，但我们在 io_worker 里设置了 curl 超时
            # 所以线程最终会自己结束，不会泄露
            raise TimeoutError("WebSocket connection timeout (15s)")

        logger.info(f"[ThreadedWS] Connected to {url} (impersonate={self._impersonate})")
        return self

    def _init_cffi_structs(self):
        from curl_cffi._wrapper import ffi
        self._cffi_objects['recv_buf_size'] = 131072
        self._cffi_objects['recv_buf'] = ffi.new("char[]", 131072)
        self._cffi_objects['n_read'] = ffi.new("size_t *")
        self._cffi_objects['frame_info'] = ffi.new("struct curl_ws_frame **")
        self._cffi_objects['n_sent'] = ffi.new("size_t *")

    def _io_worker(self):
        """核心 IO 循环"""
        from curl_cffi.requests import Session
        from curl_cffi.const import CurlInfo, CurlWsFlag, CurlECode, CurlOpt
        from curl_cffi._wrapper import lib, ffi

        session = None
        try:
            session = Session(impersonate=self._impersonate)

            # ★★★ 关键修复：设置连接超时 ★★★
            # 确保 ws_connect 不会永远卡死，防止线程泄露
            session.curl.setopt(CurlOpt.CONNECTTIMEOUT, 10) # 10秒连不上就报错
            session.curl.setopt(CurlOpt.TIMEOUT, 0) # 数据传输不设总超时

            # 代理设置
            if self._proxy:
                session.curl.setopt(CurlOpt.PROXY, self._proxy)
                logger.info(f"[ThreadedWS] Using proxy: {self._proxy}")

            # 启用 TCP Keep-Alive
            try:
                session.curl.setopt(CurlOpt.TCP_KEEPALIVE, 1)
                session.curl.setopt(CurlOpt.TCP_KEEPIDLE, 30)
                session.curl.setopt(CurlOpt.TCP_KEEPINTVL, 10)
            except Exception:
                pass

            # 建立连接 (现在这里最多卡 10 秒)
            self._ws = session.ws_connect(self._url)

            if hasattr(self._ws, 'curl'):
                self._curl_handle = self._ws.curl._curl
            else:
                self._curl_handle = self._ws._curl._curl

            self._sock_fd = self._ws.curl.getinfo(CurlInfo.ACTIVESOCKET)
            self._init_cffi_structs()
            self._last_recv_time = time.time()
            self._connected_event.set()

            # 发送状态
            current_data = None
            current_offset = 0
            n_sent_ptr = self._cffi_objects['n_sent']

            while self._running:
                # 看门狗检测
                if self._read_timeout:
                    if time.time() - self._last_recv_time > self._read_timeout:
                        raise ConnectionError(f"Read timeout (> {self._read_timeout}s)")

                rlist = [self._sock_fd]
                wlist = []
                queue_has_data = not self._send_queue.empty()
                if current_data is not None or queue_has_data:
                    wlist.append(self._sock_fd)

                try:
                    # 使用 0.5s 超时，让看门狗能有机会运行
                    rs, ws, _ = select.select(rlist, wlist, [], 0.5)
                except (OSError, ValueError):
                    break

                if self._sock_fd in rs:
                    try:
                        self._handle_recv(lib, ffi, CurlWsFlag, CurlECode)
                        self._last_recv_time = time.time()
                    except ConnectionClosed as e:
                        logger.info(f"[ThreadedWS] {e}")
                        break

                if self._sock_fd in ws:
                    if current_data is None:
                        try:
                            item = self._send_queue.get_nowait()
                            if item is None: continue
                            data, conn_id = item
                            if conn_id and conn_id in self._closed_conn_ids:
                                continue
                            current_data = data
                            current_offset = 0
                        except queue.Empty:
                            pass

                    if current_data is not None:
                        total_len = len(current_data)
                        rem_len = total_len - current_offset

                        c_buf = ffi.from_buffer(current_data)
                        c_ptr = c_buf + current_offset

                        res = lib.curl_ws_send(self._curl_handle, c_ptr, rem_len, n_sent_ptr, 0, CurlWsFlag.BINARY)
                        sent = n_sent_ptr[0]

                        if res == CurlECode.OK:
                            current_offset += sent
                            if current_offset >= total_len:
                                current_data = None
                                current_offset = 0
                        elif res == CurlECode.AGAIN:
                            pass
                        else:
                            logger.info(f"[ThreadedWS] Send failed ({res})")
                            break

        except Exception as e:
            if "ConnectionClosed" not in str(type(e)):
                logger.error(f"[ThreadedWS] IO Loop error: {e}")
                self._connect_error = e
        finally:
            self._running = False
            self._connected_event.set()
            try: self._recv_queue.put(ConnectionError("Closed"), timeout=0.1)
            except: pass
            if session:
                try: session.close()
                except: pass
            logger.debug("[ThreadedWS] IO Loop exited")

    def _handle_recv(self, lib, ffi, CurlWsFlag, CurlECode):
        buf = self._cffi_objects['recv_buf']
        buf_len = self._cffi_objects['recv_buf_size']
        p_read = self._cffi_objects['n_read']
        p_frame = self._cffi_objects['frame_info']

        while True:
            ret = lib.curl_ws_recv(self._curl_handle, buf, buf_len, p_read, p_frame)
            if ret == CurlECode.AGAIN: break
            if ret == 52: raise ConnectionClosed("Server closed connection (Empty reply/52)")
            if ret == 56: raise ConnectionClosed("Connection reset by peer (Recv error/56)")
            if ret != CurlECode.OK: raise RuntimeError(f"Recv error: {ret}")

            n_read = p_read[0]
            if n_read > 0:
                self._recv_buffer.extend(bytes(ffi.buffer(buf, n_read)))

        while len(self._recv_buffer) >= self._PROTO_HEADER_SIZE:
            try:
                data_len = struct.unpack('!I', self._recv_buffer[3:7])[0]
            except:
                self._recv_buffer.clear()
                break

            total = self._PROTO_HEADER_SIZE + data_len
            if len(self._recv_buffer) >= total:
                try: self._recv_queue.put_nowait(bytes(self._recv_buffer[:total]))
                except queue.Full: pass
                del self._recv_buffer[:total]
            else:
                break

    async def send(self, data: bytes, priority: bool = False, conn_id: Optional[bytes] = None) -> None:
        if not self._running: raise RuntimeError("WS Closed")
        if conn_id and conn_id in self._closed_conn_ids: return
        try: self._send_queue.put_nowait((data, conn_id))
        except queue.Full:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._send_queue.put, (data, conn_id))

    async def recv(self) -> bytes:
        if not self._running and self._recv_queue.empty(): raise RuntimeError("WS Closed")
        loop = asyncio.get_event_loop()
        item = await loop.run_in_executor(None, self._recv_queue.get)
        if isinstance(item, Exception): raise item
        return item.encode() if isinstance(item, str) else item

    async def mark_connection_closed(self, conn_id: bytes) -> None:
        self._closed_conn_ids.add(conn_id)

    async def close(self) -> None:
        self._running = False
        if self._io_thread: self._io_thread.join(timeout=2)

    def __aiter__(self) -> AsyncIterator: return self
    async def __anext__(self):
        try: return await self.recv()
        except: raise StopAsyncIteration