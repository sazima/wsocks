"""
WebSocket 适配器模块
支持标准 websockets 库和 curl_cffi (带 TLS 指纹伪装)
"""
import sys
import logging
from abc import ABC, abstractmethod
from typing import Optional, AsyncIterator

logger = logging.getLogger(__name__)


class WebSocketAdapter(ABC):
    """WebSocket 适配器抽象基类"""

    @abstractmethod
    async def connect(self, url: str, **kwargs) -> 'WebSocketAdapter':
        """连接到 WebSocket 服务器"""
        pass

    @abstractmethod
    async def send(self, data: bytes) -> None:
        """发送二进制数据"""
        pass

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

    async def send(self, data: bytes) -> None:
        """发送二进制数据"""
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
    """curl_cffi 适配器 (支持 TLS 指纹伪装)"""

    def __init__(self, impersonate: str = "chrome124"):
        """初始化适配器

        Args:
            impersonate: 浏览器指纹标识
                支持: chrome99-chrome136, safari153-safari260, firefox133/135
                或使用 'chrome' 自动使用最新版本
        """
        self._session = None
        self._ws = None
        self._impersonate = impersonate
        self._iterator = None

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
                "Note: curl_cffi requires Python 3.7+"
            )

        logger.debug(f"[CurlCffiAdapter] Connecting to {url} with impersonate={self._impersonate}")

        # 创建 AsyncSession 并连接
        self._session = AsyncSession(impersonate=self._impersonate)
        self._ws = await self._session.ws_connect(url)

        logger.info(f"[CurlCffiAdapter] Connected to {url} (impersonate={self._impersonate})")
        return self

    async def send(self, data: bytes) -> None:
        """发送二进制数据"""
        if self._ws is None:
            raise RuntimeError("WebSocket not connected")
        await self._ws.send_bytes(data)

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

    async def close(self) -> None:
        """关闭连接"""
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
            logger.info(f"Using CurlCffiAdapter with impersonate={impersonate}")
            return CurlCffiAdapter(impersonate=impersonate)
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
            logger.info("Auto-detected: Using CurlCffiAdapter (TLS fingerprinting enabled)")
            return CurlCffiAdapter()
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
