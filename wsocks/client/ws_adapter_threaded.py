"""
WebSocket 适配器 - 线程版本
每个 WebSocket 连接运行在独立线程中，使用同步 API
彻底解决 asyncio 事件循环竞争导致的 52 错误
"""
import asyncio
import threading
import queue
import logging
import time
from typing import Optional, AsyncIterator

from wsocks.common.logger import setup_logger

logger = setup_logger()

REQUIRED_CURL_CFFI_VERSION = '0.14.0'


class ThreadedCurlWebSocket:
    """线程化的 curl_cffi WebSocket 适配器

    架构：
    - 主线程：asyncio 事件循环（SOCKS5 服务器）
    - 工作线程：每个 WebSocket 连接一个独立线程
    - 通信：queue.Queue（线程安全）

    优势：
    - 每个连接独立轮询，无竞争
    - libcurl 同步 API，符合设计
    - 彻底避免 52 错误
    """

    def __init__(self, impersonate: str = "chrome124"):
        """初始化适配器

        Args:
            impersonate: 浏览器指纹标识
        """
        self._impersonate = impersonate
        self._url = None

        # 线程间通信队列
        self._send_queue_high = queue.Queue(maxsize=512)    # 高优先级发送
        self._send_queue_normal = queue.Queue(maxsize=4096) # 普通发送
        self._recv_queue = queue.Queue(maxsize=512)         # 接收

        # 工作线程
        self._send_thread = None
        self._recv_thread = None
        self._running = False

        # WebSocket 对象（在工作线程中创建）
        self._ws = None
        self._ws_ready = threading.Event()  # 连接完成信号
        self._connect_error = None

        # 已关闭连接过滤（延迟清理策略）
        # 格式: {conn_id: close_timestamp}
        self._closed_conn_ids = {}
        self._closed_conn_lock = threading.Lock()
        self._closed_retention = 30  # 保留30秒后自动清理

        # 用于 asyncio 集成（不需要显式 executor，使用 asyncio 默认的）

    async def connect(self, url: str, **kwargs) -> 'ThreadedCurlWebSocket':
        """连接到 WebSocket 服务器

        Args:
            url: WebSocket URL
            **kwargs: 其他参数（暂时忽略）
        """
        try:
            import curl_cffi as _curl_cffi
            installed_version = getattr(_curl_cffi, "__version__", "unknown")
            if installed_version != REQUIRED_CURL_CFFI_VERSION:
                raise ImportError(
                    f"Unsupported curl_cffi version: {installed_version} "
                    f"(required: {REQUIRED_CURL_CFFI_VERSION})"
                )
        except ImportError as e:
            raise ImportError("curl_cffi not installed or version mismatch") from e

        self._url = url
        self._running = True

        logger.debug(f"[ThreadedWS] Connecting to {url} with impersonate={self._impersonate}")

        # 在独立线程中建立连接
        connect_thread = threading.Thread(target=self._connect_worker, daemon=True)
        connect_thread.start()

        # 等待连接完成（在 executor 中等待，避免阻塞 asyncio）
        loop = asyncio.get_event_loop()

        # 使用 asyncio 默认的 ThreadPoolExecutor（传 None）
        success = await loop.run_in_executor(None, self._ws_ready.wait, 10)  # 10秒超时

        if self._connect_error:
            raise self._connect_error

        if not success:
            raise TimeoutError("WebSocket connection timeout (10s)")

        logger.info(f"[ThreadedWS] Connected to {url} (impersonate={self._impersonate})")
        return self

    def _connect_worker(self):
        """在独立线程中建立连接（同步）"""
        try:
            from curl_cffi.requests import Session

            # 创建同步 Session 和 WebSocket
            session = Session(impersonate=self._impersonate)
            self._ws = session.ws_connect(self._url)

            # 标记连接成功
            self._ws_ready.set()

            # 启动收发线程
            self._recv_thread = threading.Thread(target=self._recv_worker, daemon=True)
            self._recv_thread.start()

            self._send_thread = threading.Thread(target=self._send_worker, daemon=True)
            self._send_thread.start()

        except Exception as e:
            logger.error(f"[ThreadedWS] Connection error: {e}")
            self._connect_error = e
            self._ws_ready.set()

    def _recv_worker(self):
        """接收线程：持续从 WebSocket 读取数据"""
        logger.debug("[ThreadedWS] Recv worker started")

        try:
            # 持续接收（阻塞，但不会影响其他连接）
            for message in self._ws:
                if not self._running:
                    break

                # 放入接收队列
                try:
                    self._recv_queue.put(message, timeout=1)
                except queue.Full:
                    logger.warning("[ThreadedWS] Recv queue full, dropping message")

        except Exception as e:
            logger.error(f"[ThreadedWS] Recv worker error: {e}")
            # 将异常放入队列，让上层处理
            try:
                self._recv_queue.put(e, timeout=1)
            except queue.Full:
                pass
        finally:
            logger.debug("[ThreadedWS] Recv worker stopped")

    def _send_worker(self):
        """发送线程：从队列取数据并发送（支持优先级）"""
        logger.debug("[ThreadedWS] Send worker started")

        batch_size = 32

        try:
            while self._running:
                # 定期清理过期的已关闭连接ID（延迟清理策略）
                now = time.time()
                with self._closed_conn_lock:
                    expired = [
                        cid for cid, close_time in self._closed_conn_ids.items()
                        if now - close_time > self._closed_retention
                    ]
                    for cid in expired:
                        del self._closed_conn_ids[cid]

                    if expired:
                        logger.debug(f"[ThreadedWS] Cleaned up {len(expired)} expired closed conn_ids")

                batch = []

                # 1. 优先从高优先级队列获取
                while len(batch) < batch_size:
                    try:
                        item = self._send_queue_high.get_nowait()
                        batch.append(item)
                    except queue.Empty:
                        break

                # 2. 从普通队列获取
                if not batch:
                    try:
                        item = self._send_queue_normal.get(timeout=0.1)
                        batch.append(item)
                    except queue.Empty:
                        continue

                    # 批量获取剩余
                    while len(batch) < batch_size:
                        try:
                            item = self._send_queue_normal.get_nowait()
                            batch.append(item)
                        except queue.Empty:
                            break

                # 3. 批量发送
                for data, conn_id in batch:
                    # 检查连接是否已关闭
                    if conn_id:
                        with self._closed_conn_lock:
                            if conn_id in self._closed_conn_ids:
                                logger.debug(f"[ThreadedWS] Skip sending for closed connection {conn_id.hex()}")
                                continue

                    # 发送数据（同步，阻塞）
                    try:
                        self._ws.send_bytes(data)
                    except Exception as e:
                        logger.error(f"[ThreadedWS] Send error: {e}")
                        # 发送失败，继续处理下一个

        except Exception as e:
            logger.error(f"[ThreadedWS] Send worker error: {e}")
        finally:
            logger.debug("[ThreadedWS] Send worker stopped")

    async def send(self, data: bytes, priority: bool = False, conn_id: Optional[bytes] = None) -> None:
        """发送数据（异步接口，实际放入队列）

        Args:
            data: 要发送的数据
            priority: True=高优先级（心跳/控制消息），False=普通优先级
            conn_id: 连接ID（可选），用于关闭时过滤
        """
        if not self._running:
            raise RuntimeError("WebSocket not connected")

        # 发送前检查该连接是否已关闭
        if conn_id:
            with self._closed_conn_lock:
                if conn_id in self._closed_conn_ids:
                    logger.debug(f"[ThreadedWS] Skip sending for closed connection {conn_id.hex()}")
                    return

        # 选择队列
        q = self._send_queue_high if priority else self._send_queue_normal

        # 非阻塞放入队列
        try:
            q.put_nowait((data, conn_id))
        except queue.Full:
            # 队列满，阻塞等待（在 executor 中）
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, q.put, (data, conn_id))

    async def recv(self) -> bytes:
        """接收数据（异步接口，实际从队列读取）"""
        if not self._running:
            raise RuntimeError("WebSocket not connected")

        # 从队列读取（在 executor 中阻塞等待）
        loop = asyncio.get_event_loop()
        message = await loop.run_in_executor(None, self._recv_queue.get)

        # 检查是否是异常
        if isinstance(message, Exception):
            raise message

        # 统一返回 bytes
        if isinstance(message, bytes):
            return message
        elif isinstance(message, str):
            return message.encode()
        else:
            raise TypeError(f"Unexpected message type: {type(message)}")

    async def mark_connection_closed(self, conn_id: bytes) -> None:
        """标记连接已关闭，队列中该连接的消息将被丢弃

        Args:
            conn_id: 要标记为关闭的连接ID
        """
        with self._closed_conn_lock:
            # 记录关闭时间戳（延迟清理策略）
            self._closed_conn_ids[conn_id] = time.time()
            logger.debug(f"[ThreadedWS] Marked connection {conn_id.hex()} as closed (will auto-cleanup after {self._closed_retention}s)")

    async def close(self) -> None:
        """关闭连接"""
        logger.debug("[ThreadedWS] Closing connection")
        self._running = False

        # 等待工作线程结束（最多3秒）
        if self._send_thread and self._send_thread.is_alive():
            self._send_thread.join(timeout=3)

        if self._recv_thread and self._recv_thread.is_alive():
            self._recv_thread.join(timeout=3)

        # 关闭 WebSocket
        if self._ws:
            try:
                self._ws.close()
            except Exception as e:
                logger.debug(f"[ThreadedWS] Error closing WebSocket: {e}")
            finally:
                self._ws = None

        logger.debug("[ThreadedWS] Connection closed")

    def __aiter__(self) -> AsyncIterator:
        """异步迭代器支持"""
        if not self._running:
            raise RuntimeError("WebSocket not connected")
        return self

    async def __anext__(self):
        """异步迭代器支持"""
        try:
            return await self.recv()
        except Exception:
            raise StopAsyncIteration
