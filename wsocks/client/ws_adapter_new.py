"""
WebSocket 适配器模块 - 重构版
直接使用底层 libcurl API，绕过 curl_cffi 的 AsyncWebSocket 封装
解决高延迟 + TLS fingerprint 场景下的 52 错误问题
"""
import asyncio
import logging
from typing import Optional, AsyncIterator
from wsocks.common.logger import setup_logger

logger = setup_logger()

REQUIRED_CURL_CFFI_VERSION = '0.14.0'


class CurlCffiAdapterDirect:
    """直接使用底层 libcurl WebSocket API 的适配器

    关键改进：
    1. 绕过 curl_cffi.AsyncWebSocket 的封装
    2. 自己实现事件循环，精确控制 PING/PONG 处理
    3. 52 错误时短暂等待并重试（libcurl 可能正在处理 PING）
    4. 可选择忽略 PING 帧，完全依赖应用层心跳
    """

    def __init__(self, impersonate: str = "chrome124", ignore_ping: bool = True):
        """初始化适配器

        Args:
            impersonate: 浏览器指纹标识
            ignore_ping: 是否忽略 PING 帧（True=完全依赖应用层心跳）
        """
        self._curl = None
        self._sock_fd = -1
        self._impersonate = impersonate
        self._ignore_ping = ignore_ping
        self._loop = None
        self._closed = False

        # 接收队列（后台持续读取）
        self._receive_queue = asyncio.Queue(maxsize=512)
        self._read_task = None

        # 发送队列（双优先级）
        self._high_priority_queue = asyncio.Queue(maxsize=512)
        self._normal_priority_queue = asyncio.Queue(maxsize=4096)
        self._write_task = None
        self._batch_send_size = 32

        # 已关闭连接过滤
        self._closed_conn_ids = set()
        self._closed_conn_lock = asyncio.Lock()

        # 预分配 CFFI 缓冲区
        from curl_cffi._wrapper import ffi
        self._ws_recv_buffer_size = 128 * 1024
        self._ws_recv_buffer = ffi.new("char[]", self._ws_recv_buffer_size)
        self._ws_recv_n_recv = ffi.new("size_t *")
        self._ws_recv_p_frame = ffi.new("struct curl_ws_frame **")
        self._ws_send_n_sent = ffi.new("size_t *")

    async def connect(self, url: str, **kwargs):
        """连接到 WebSocket 服务器"""
        try:
            from curl_cffi.curl import Curl
            from curl_cffi.const import CurlOpt, CurlInfo
            import curl_cffi as _curl_cffi

            installed_version = getattr(_curl_cffi, "__version__", "unknown")
            if installed_version != REQUIRED_CURL_CFFI_VERSION:
                raise ImportError(f"Unsupported curl_cffi version: {installed_version}")
        except ImportError as e:
            raise ImportError("curl_cffi not installed or version mismatch") from e

        logger.debug(f"[CurlCffiDirect] Connecting to {url} with impersonate={self._impersonate}")

        # 创建 Curl 对象
        self._curl = Curl(debug=False)

        # 先设置 TLS 指纹（必须在其他选项之前）
        if self._impersonate:
            self._curl.impersonate(self._impersonate, default_headers=True)

        # 设置 URL
        self._curl.setopt(CurlOpt.URL, url)

        # WebSocket 连接模式（CONNECT_ONLY = 2）
        self._curl.setopt(CurlOpt.CONNECT_ONLY, 2)

        # 禁用 Nagle 算法
        self._curl.setopt(CurlOpt.TCP_NODELAY, 1)

        # 确保 CA 证书
        self._curl._ensure_cacert()

        # 执行连接（同步，在线程池中执行）
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._curl.perform, False, False)

        # 获取 socket fd
        self._sock_fd = self._curl.getinfo(CurlInfo.ACTIVESOCKET)
        if self._sock_fd < 0:
            raise RuntimeError(f"Invalid socket fd: {self._sock_fd}")

        self._loop = loop

        # 启动后台循环
        self._read_task = asyncio.create_task(self._read_loop())
        self._write_task = asyncio.create_task(self._write_loop())

        logger.info(f"[CurlCffiDirect] Connected to {url} (sock_fd={self._sock_fd}, ignore_ping={self._ignore_ping})")
        return self

    async def _read_loop(self):
        """后台持续读取循环

        关键：正确处理 52 错误和 PING 帧
        """
        from curl_cffi._wrapper import lib, ffi
        from curl_cffi.const import CurlECode, CurlWsFlag

        chunks = []
        error_52_consecutive = 0  # 连续 52 错误计数
        max_52_consecutive = 10   # 允许最多 10 次连续 52（可能是 PING 处理）

        try:
            while not self._closed:
                # 等待 socket 可读
                read_future = self._loop.create_future()
                try:
                    self._loop.add_reader(self._sock_fd, read_future.set_result, None)
                    await read_future
                finally:
                    self._loop.remove_reader(self._sock_fd)

                # 持续读取
                while True:
                    ret = lib.curl_ws_recv(
                        self._curl._curl,
                        self._ws_recv_buffer,
                        self._ws_recv_buffer_size,
                        self._ws_recv_n_recv,
                        self._ws_recv_p_frame
                    )

                    # EAGAIN - 正常，等待下次可读
                    if ret == CurlECode.AGAIN:
                        error_52_consecutive = 0  # 重置 52 计数
                        break

                    # 错误 52 - 诊断到底是什么问题
                    if ret == 52:
                        error_52_consecutive += 1

                        # ===== 诊断：直接读socket看是否有数据 =====
                        import socket as sock_module
                        import select

                        # 1. 用select检查
                        readable, _, _ = select.select([self._sock_fd], [], [], 0)

                        # 2. 尝试直接peek socket
                        raw_data = None
                        try:
                            test_sock = sock_module.socket(fileno=self._sock_fd)
                            test_sock.setblocking(False)
                            raw_data = test_sock.recv(16, sock_module.MSG_PEEK)
                        except:
                            pass

                        logger.warning(
                            f"[CurlCffiDirect] Error 52 (#{error_52_consecutive}): "
                            f"select_readable={bool(readable)}, "
                            f"raw_peek={len(raw_data) if raw_data else 0} bytes, "
                            f"raw_hex={raw_data.hex() if raw_data else 'N/A'}"
                        )
                        # ===== 诊断结束 =====

                        if error_52_consecutive > max_52_consecutive:
                            logger.error(f"[CurlCffiDirect] Error 52 exceeded {max_52_consecutive} times")
                            error = ConnectionError("WebSocket error 52 - connection unstable")
                            await self._receive_queue.put(error)
                            return

                        # 短暂等待后重试
                        await asyncio.sleep(0.005)  # 5ms
                        continue  # 重试

                    # 其他错误
                    if ret != 0:
                        error = RuntimeError(f"curl_ws_recv failed: error code {ret}")
                        await self._receive_queue.put(error)
                        logger.error(f"[CurlCffiDirect] Recv error: {error}")
                        return

                    # 成功读取
                    error_52_consecutive = 0  # 重置 52 计数

                    n_recv = self._ws_recv_n_recv[0]
                    if n_recv > 0:
                        chunk = bytes(ffi.buffer(self._ws_recv_buffer)[:n_recv])
                        frame = self._ws_recv_p_frame[0]

                        # 处理 CLOSE 帧
                        if frame.flags & CurlWsFlag.CLOSE:
                            logger.debug("[CurlCffiDirect] Received CLOSE frame")
                            error = ConnectionError("WebSocket CLOSE frame received")
                            await self._receive_queue.put(error)
                            return

                        # 处理 PING 帧
                        if frame.flags & CurlWsFlag.PING:
                            if self._ignore_ping:
                                # 忽略 PING，依赖应用层心跳
                                logger.debug("[CurlCffiDirect] Ignored PING frame (using app-layer heartbeat)")
                                continue
                            else:
                                # TODO: 手动发送 PONG（如果需要）
                                logger.debug("[CurlCffiDirect] Received PING frame")
                                continue

                        # 处理 PONG 帧（忽略）
                        if frame.flags & (1 << 5):  # PONG flag
                            logger.debug("[CurlCffiDirect] Ignored PONG frame")
                            continue

                        # 收集数据帧
                        chunks.append(chunk)

                        # 完整消息
                        if frame.bytesleft == 0 and (frame.flags & CurlWsFlag.CONT) == 0:
                            message = b''.join(chunks)
                            chunks = []
                            await self._receive_queue.put(message)

        except asyncio.CancelledError:
            logger.debug("[CurlCffiDirect] Read loop cancelled")
        except Exception as e:
            logger.error(f"[CurlCffiDirect] Read loop error: {e}")
            import traceback
            traceback.print_exc()

    async def recv(self) -> bytes:
        """从接收队列获取数据"""
        if self._curl is None or self._sock_fd < 0:
            raise RuntimeError("WebSocket not connected")

        message = await self._receive_queue.get()

        if isinstance(message, Exception):
            raise message

        return message

    async def send(self, data: bytes, priority: bool = False, conn_id: Optional[bytes] = None) -> None:
        """发送数据"""
        if self._curl is None or self._closed:
            raise RuntimeError("WebSocket not connected")

        # 检查连接是否已关闭
        if conn_id:
            async with self._closed_conn_lock:
                if conn_id in self._closed_conn_ids:
                    return

        # 选择队列
        queue = self._high_priority_queue if priority else self._normal_priority_queue

        try:
            queue.put_nowait((data, conn_id))
        except asyncio.QueueFull:
            await queue.put((data, conn_id))

    async def mark_connection_closed(self, conn_id: bytes) -> None:
        """标记连接已关闭"""
        async with self._closed_conn_lock:
            self._closed_conn_ids.add(conn_id)

    async def _write_loop(self):
        """发送循环（优先级队列）"""
        from curl_cffi._wrapper import lib
        from curl_cffi.const import CurlWsFlag, CurlECode

        try:
            while not self._closed:
                batch = []

                # 1. 优先从高优先级队列获取
                while len(batch) < self._batch_send_size:
                    try:
                        item = self._high_priority_queue.get_nowait()
                        batch.append(item)
                    except asyncio.QueueEmpty:
                        break

                # 2. 从普通队列获取
                if not batch:
                    item = await self._normal_priority_queue.get()
                    batch.append(item)

                    while len(batch) < self._batch_send_size:
                        try:
                            item = self._normal_priority_queue.get_nowait()
                            batch.append(item)
                        except asyncio.QueueEmpty:
                            break

                # 3. 批量发送
                for data, conn_id in batch:
                    # 检查连接是否已关闭
                    if conn_id:
                        async with self._closed_conn_lock:
                            if conn_id in self._closed_conn_ids:
                                continue

                    # 发送数据
                    await self._direct_send(data)

        except asyncio.CancelledError:
            logger.debug("[CurlCffiDirect] Write loop cancelled")
        except Exception as e:
            logger.error(f"[CurlCffiDirect] Write loop error: {e}")

    async def _direct_send(self, data: bytes) -> None:
        """直接发送数据"""
        from curl_cffi._wrapper import lib, ffi
        from curl_cffi.const import CurlWsFlag, CurlECode

        offset = 0
        max_frame_size = 65535

        while offset < len(data):
            chunk_size = min(len(data) - offset, max_frame_size)
            chunk = data[offset:offset + chunk_size]
            buffer = ffi.from_buffer(chunk)

            while True:
                ret = lib.curl_ws_send(
                    self._curl._curl,
                    buffer,
                    len(chunk),
                    self._ws_send_n_sent,
                    0,
                    CurlWsFlag.BINARY
                )

                if ret == 0:
                    n_sent = self._ws_send_n_sent[0]
                    if n_sent == 0:
                        raise RuntimeError("curl_ws_send returned 0 bytes")
                    offset += n_sent
                    break

                elif ret == CurlECode.AGAIN:
                    # 等待可写
                    write_future = self._loop.create_future()
                    try:
                        self._loop.add_writer(self._sock_fd, write_future.set_result, None)
                        await write_future
                    finally:
                        self._loop.remove_writer(self._sock_fd)
                    continue

                else:
                    raise RuntimeError(f"curl_ws_send failed: error code {ret}")

    async def close(self) -> None:
        """关闭连接"""
        self._closed = True

        # 停止后台任务
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass

        if self._write_task:
            self._write_task.cancel()
            try:
                await self._write_task
            except asyncio.CancelledError:
                pass

        # 移除事件监听
        if self._loop and self._sock_fd >= 0:
            try:
                self._loop.remove_reader(self._sock_fd)
            except Exception:
                pass
            try:
                self._loop.remove_writer(self._sock_fd)
            except Exception:
                pass

        # 关闭 curl
        if self._curl:
            try:
                self._curl.close()
            except Exception as e:
                logger.debug(f"[CurlCffiDirect] Error closing curl: {e}")
            finally:
                self._curl = None

        self._sock_fd = -1

    def __aiter__(self) -> AsyncIterator:
        """异步迭代器支持"""
        if self._curl is None or self._sock_fd < 0:
            raise RuntimeError("WebSocket not connected")
        return self

    async def __anext__(self):
        """异步迭代器支持"""
        try:
            return await self.recv()
        except Exception:
            raise StopAsyncIteration
