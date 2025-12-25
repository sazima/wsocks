"""
WebSocket 适配器模块
支持标准 websockets 库和 curl_cffi (带 TLS 指纹伪装)
"""
import asyncio
import sys
import logging
from abc import ABC, abstractmethod
from typing import Optional, AsyncIterator

from wsocks.common.logger import setup_logger

logger = setup_logger()

REQUIRED_CURL_CFFI_VERSION = '0.14.0'


class WebSocketAdapter(ABC):
    """WebSocket 适配器抽象基类"""

    @abstractmethod
    async def connect(self, url: str, **kwargs) -> 'WebSocketAdapter':
        """连接到 WebSocket 服务器"""
        pass

    @abstractmethod
    async def send(self, data: bytes, priority: bool = False, conn_id: Optional[bytes] = None) -> None:
        """发送二进制数据

        Args:
            data: 要发送的数据
            priority: 是否高优先级（心跳/控制消息应设为 True）
            conn_id: 连接ID（可选，用于关闭时过滤队列中的消息）
        """
        pass

    async def mark_connection_closed(self, conn_id: bytes) -> None:
        """标记连接已关闭，队列中该连接的消息将被丢弃

        Args:
            conn_id: 要标记为关闭的连接ID
        """
        pass  # 默认实现为空，子类可选择性实现

    @abstractmethod
    async def recv(self) -> bytes:
        """接收数据"""
        pass

    @abstractmethod
    async def close(self) -> None:
        """关闭连接"""
        pass

    @abstractmethod
    def __aiter__(self) -> AsyncIterator:
        """异步迭代器支持"""
        pass

    @abstractmethod
    async def __anext__(self):
        """异步迭代器支持"""
        pass


class WebSocketsAdapter(WebSocketAdapter):
    """标准 websockets 库适配器"""

    def __init__(self):
        self._ws = None
        self._iterator = None

    async def connect(self, url: str, **kwargs) -> 'WebSocketsAdapter':
        """连接到 WebSocket 服务器

        Args:
            url: WebSocket URL
            **kwargs: 支持 ping_interval, ping_timeout, compression 等参数
        """
        import websockets

        # 提取支持的参数
        ping_interval = kwargs.get('ping_interval')
        ping_timeout = kwargs.get('ping_timeout')
        compression = kwargs.get('compression')

        logger.debug(f"[WebSocketsAdapter] Connecting to {url}")

        self._ws = await websockets.connect(
            url,
            ping_interval=ping_interval,
            ping_timeout=ping_timeout,
            compression=compression
        )

        logger.info(f"[WebSocketsAdapter] Connected to {url}")
        return self

    async def send(self, data: bytes, priority: bool = False, conn_id: Optional[bytes] = None) -> None:
        """发送二进制数据

        Args:
            data: 要发送的数据
            priority: 忽略（websockets 库不支持优先级）
            conn_id: 忽略（websockets 库直接发送，不需要队列过滤）
        """
        if self._ws is None:
            raise RuntimeError("WebSocket not connected")
        await self._ws.send(data)

    async def recv(self) -> bytes:
        """接收数据"""
        if self._ws is None:
            raise RuntimeError("WebSocket not connected")
        return await self._ws.recv()

    async def close(self) -> None:
        """关闭连接"""
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    def __aiter__(self) -> AsyncIterator:
        """异步迭代器支持"""
        if self._ws is None:
            raise RuntimeError("WebSocket not connected")
        self._iterator = self._ws.__aiter__()
        return self

    async def __anext__(self):
        """异步迭代器支持"""
        if self._iterator is None:
            raise RuntimeError("Iterator not initialized")
        return await self._iterator.__anext__()


class CurlCffiAdapter(WebSocketAdapter):
    """curl_cffi 适配器 (支持 TLS 指纹伪装)

    使用双优先级队列：
    - 高优先级队列：心跳、控制消息（立即发送）
    - 普通队列：业务数据（正常排队）
    """

    def __init__(self, impersonate: str = "chrome124"):
        """初始化适配器

        Args:
            impersonate: 浏览器指纹标识
                支持: chrome99-chrome136, safari153-safari260, firefox133/135
                或使用 'chrome' 自动使用最新版本
        """
        self._session = None  # type: 'AsyncSession'
        self._ws = None
        self._impersonate = impersonate
        self._iterator = None

        # 双队列系统（存储格式：(data: bytes, conn_id: Optional[bytes])）
        self._high_priority_queue = asyncio.Queue(maxsize=512)  # 心跳/控制消息
        self._normal_priority_queue = asyncio.Queue(maxsize=4096)  # 业务数据（增大以支持高吞吐量上传）
        self._write_task = None  # 发送循环任务
        self._closed = False
        self._batch_send_size = 32  # 批量发送大小（调大以提升上传性能）

        # 已关闭连接的跟踪（用于过滤队列中的消息）
        self._closed_conn_ids = set()  # type: set[bytes]
        self._closed_conn_lock = asyncio.Lock()

    async def connect(self, url: str, **kwargs) -> 'CurlCffiAdapter':
        """连接到 WebSocket 服务器

        Args:
            url: WebSocket URL
            **kwargs: 其他参数 (curl_cffi 的 WebSocket 参数较少)
        """
        try:
            from curl_cffi.requests import AsyncSession
        except ImportError:
            raise ImportError(
                "curl_cffi is not installed. Install it with: pip install curl_cffi\n"
                "Note: curl_cffi requires Python 3.9+"
            )
        try:
            import curl_cffi as _curl_cffi
            installed_version = getattr(_curl_cffi, "__version__", "unknown")
        except Exception:
            installed_version = "unknown"
        if installed_version != REQUIRED_CURL_CFFI_VERSION:
            raise ImportError(
                f"Unsupported curl_cffi version: {installed_version} (required: {REQUIRED_CURL_CFFI_VERSION}).\n"
                f"To install the required version, run:\n"
                f"    pip install --upgrade \"curl_cffi=={REQUIRED_CURL_CFFI_VERSION}\"\n"
                "Or install a different version and adjust your environment accordingly."
            )

        logger.debug(f"[CurlCffiAdapter] Connecting to {url} with impersonate={self._impersonate}")
        # 创建 AsyncSession 并连接
        self._session = AsyncSession(impersonate=self._impersonate)
        self._ws = await self._session.ws_connect(url)

        # 启动优先级发送循环
        self._write_task = asyncio.create_task(self._priority_write_loop())

        logger.info(f"[CurlCffiAdapter] Connected to {url} (impersonate={self._impersonate})")
        return self

    async def send(self, data: bytes, priority: bool = False, conn_id: Optional[bytes] = None) -> None:
        """发送二进制数据（通过优先级队列）

        Args:
            data: 要发送的二进制数据
            priority: True=高优先级（心跳/控制消息），False=普通优先级（业务数据）
            conn_id: 连接ID（可选），用于关闭连接时过滤队列中的消息

        Raises:
            RuntimeError: WebSocket 未连接
        """
        if self._ws is None or self._closed:
            raise RuntimeError("WebSocket not connected")

        # 发送前检查该连接是否已关闭
        if conn_id:
            async with self._closed_conn_lock:
                if conn_id in self._closed_conn_ids:
                    logger.debug(f"[WS] Skip sending for closed connection {conn_id.hex()}")
                    return  # 不发送

        # 根据优先级选择队列
        queue = self._high_priority_queue if priority else self._normal_priority_queue

        try:
            # 非阻塞入队（如果队列满则立即失败）
            queue.put_nowait((data, conn_id))
        except asyncio.QueueFull:
            # 队列满时阻塞等待
            await queue.put((data, conn_id))

    async def mark_connection_closed(self, conn_id: bytes) -> None:
        """标记连接已关闭，队列中该连接的消息将被丢弃

        Args:
            conn_id: 要标记为关闭的连接ID
        """
        async with self._closed_conn_lock:
            self._closed_conn_ids.add(conn_id)
            logger.debug(f"[WS] Marked connection {conn_id.hex()} as closed, queued messages will be filtered")

    async def recv(self) -> bytes:
        """接收数据"""
        if self._ws is None:
            raise RuntimeError("WebSocket not connected")

        # curl_cffi 通过迭代接收消息
        message = await self._ws.__anext__()

        # 根据消息类型返回数据
        if isinstance(message, bytes):
            return message
        elif isinstance(message, str):
            return message.encode()
        else:
            # 可能是 aiohttp.WSMessage 对象
            if hasattr(message, 'data'):
                data = message.data
                if isinstance(data, bytes):
                    return data
                elif isinstance(data, str):
                    return data.encode()

        raise TypeError(f"Unexpected message type: {type(message)}")

    async def _priority_write_loop(self):
        """优先级发送循环（单线程，无需锁）

        策略：
        1. 优先检查高优先级队列（非阻塞）
        2. 批量获取多个消息以提升发送效率
        3. 发送前过滤已关闭连接的消息
        4. 单线程发送，天然串行，无需锁
        """
        try:
            while not self._closed:
                batch = []

                # 1. 优先从高优先级队列批量获取
                while len(batch) < self._batch_send_size:
                    try:
                        item = self._high_priority_queue.get_nowait()
                        batch.append(item)
                    except asyncio.QueueEmpty:
                        break

                # 2. 如果高优先级队列为空，从普通队列批量获取
                if not batch:
                    # 至少等待一个消息
                    try:
                        item = await self._normal_priority_queue.get()
                        batch.append(item)
                    except asyncio.CancelledError:
                        break

                    # 批量获取剩余消息（非阻塞）
                    while len(batch) < self._batch_send_size:
                        try:
                            item = self._normal_priority_queue.get_nowait()
                            batch.append(item)
                        except asyncio.QueueEmpty:
                            break

                # 3. 批量发送
                for item in batch:
                    data, conn_id = item

                    # 检查连接是否已关闭
                    if conn_id:
                        async with self._closed_conn_lock:
                            if conn_id in self._closed_conn_ids:
                                logger.debug(f"[WS] Discard queued message for closed connection {conn_id.hex()}")
                                continue  # 跳过，不发送

                    # 发送数据
                    try:
                        await self._direct_send(data)
                    except Exception as e:
                        logger.error(f"[CurlCffiAdapter] Send error in write loop: {e}")
                        # 发送失败，继续处理下一个

        except asyncio.CancelledError:
            logger.debug("[CurlCffiAdapter] Write loop cancelled")
        except Exception as e:
            logger.error(f"[CurlCffiAdapter] Write loop error: {e}")
            import traceback
            traceback.print_exc()

    async def _direct_send(self, data: bytes) -> None:
        """直接发送数据到 WebSocket（底层实现）

        此方法只被 _priority_write_loop 单线程调用，无需锁

        Args:
            data: 要发送的数据

        Raises:
            Exception: 发送失败
        """
        from curl_cffi.const import CurlWsFlag, CurlECode

        loop = self._ws.loop
        sock_fd = self._ws._sock_fd
        curl_ws_send = self._ws._curl.ws_send

        offset = 0
        while offset < len(data):
            try:
                # 直接发送（分片处理大数据）
                chunk = data[offset:offset + 65535]  # libcurl 最大帧大小
                n_sent = curl_ws_send(chunk, CurlWsFlag.BINARY)
                offset += n_sent

            except Exception as e:
                # 处理 EAGAIN 错误 - socket 缓冲区满
                if hasattr(e, 'code') and e.code == CurlECode.AGAIN:
                    # 等待 socket 变为可写状态
                    write_future = loop.create_future()
                    try:
                        loop.add_writer(sock_fd, write_future.set_result, None)
                        await write_future
                    finally:
                        loop.remove_writer(sock_fd)
                    continue  # 重试发送
                else:
                    # 其他错误直接抛出
                    raise

    async def close(self) -> None:
        """关闭连接"""
        self._closed = True

        # 停止发送循环
        if self._write_task is not None:
            self._write_task.cancel()
            try:
                await self._write_task
            except asyncio.CancelledError:
                pass
            self._write_task = None

        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception as e:
                logger.debug(f"[CurlCffiAdapter] Error closing WebSocket: {e}")
            finally:
                self._ws = None

        if self._session is not None:
            try:
                await self._session.close()
            except Exception as e:
                logger.debug(f"[CurlCffiAdapter] Error closing session: {e}")
            finally:
                self._session = None

    def __aiter__(self) -> AsyncIterator:
        """异步迭代器支持"""
        if self._ws is None:
            raise RuntimeError("WebSocket not connected")
        self._iterator = self._ws.__aiter__()
        return self

    async def __anext__(self):
        """异步迭代器支持"""
        if self._iterator is None:
            raise RuntimeError("Iterator not initialized")

        message = await self._iterator.__anext__()

        # 统一返回 bytes
        if isinstance(message, bytes):
            return message
        elif isinstance(message, str):
            return message.encode()
        else:
            # 可能是 aiohttp.WSMessage 对象
            if hasattr(message, 'data'):
                data = message.data
                if isinstance(data, bytes):
                    return data
                elif isinstance(data, str):
                    return data.encode()

        raise TypeError(f"Unexpected message type: {type(message)}")


def create_ws_adapter(
    use_fingerprint: bool = False,
    impersonate: str = "chrome124"
) -> WebSocketAdapter:
    """创建 WebSocket 适配器

    Args:
        use_fingerprint: 是否启用 TLS 指纹伪装
        impersonate: 浏览器指纹标识 (仅当 use_fingerprint=True 时有效)
            支持: chrome99-chrome136, safari153-safari260, firefox133/135

    Returns:
        WebSocketAdapter: 适配器实例

    Raises:
        RuntimeError: 如果 Python 版本不支持 curl_cffi
    """
    if use_fingerprint:
        # 检查 Python 版本
        if sys.version_info < (3, 7):
            raise RuntimeError(
                f"TLS fingerprinting requires Python 3.7+, current: {sys.version_info.major}.{sys.version_info.minor}\n"
                "Please upgrade Python or disable fingerprinting (use_fingerprint=False)"
            )

        # 检查 curl_cffi 是否可用
        try:
            import curl_cffi
            from .ws_adapter_threaded import ThreadedCurlWebSocket
            logger.info(f"Using ThreadedCurlWebSocket with impersonate={impersonate}")
            return ThreadedCurlWebSocket(impersonate=impersonate)
        except ImportError:
            raise ImportError(
                "curl_cffi is not installed. Install it with: pip install curl_cffi\n"
                "Or disable fingerprinting (use_fingerprint=False)"
            )
    else:
        logger.info("Using WebSocketsAdapter (standard)")
        return WebSocketsAdapter()


# 自动检测最佳适配器
def auto_detect_adapter(prefer_fingerprint: bool = False) -> WebSocketAdapter:
    """自动检测并选择最佳适配器

    Args:
        prefer_fingerprint: 如果可用，优先使用 TLS 指纹伪装

    Returns:
        WebSocketAdapter: 适配器实例
    """
    if prefer_fingerprint and sys.version_info >= (3, 7):
        try:
            import curl_cffi
            from .ws_adapter_threaded import ThreadedCurlWebSocket
            logger.info("Auto-detected: Using ThreadedCurlWebSocket (TLS fingerprinting enabled)")
            return ThreadedCurlWebSocket()
        except ImportError:
            logger.info("curl_cffi not available, falling back to WebSocketsAdapter")
            return WebSocketsAdapter()
    else:
        if prefer_fingerprint and sys.version_info < (3, 7):
            logger.warning(
                f"TLS fingerprinting requires Python 3.7+, current: {sys.version_info.major}.{sys.version_info.minor}. "
                "Using standard WebSocketsAdapter"
            )
        logger.info("Using WebSocketsAdapter (standard)")
        return WebSocketsAdapter()
