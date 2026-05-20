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

        proxy = kwargs.get('proxy')

        connect_kwargs = dict(
            ping_interval=ping_interval,
            ping_timeout=ping_timeout,
            compression=compression,
        )
        if proxy:
            connect_kwargs['proxy'] = proxy
            logger.info("[WebSocketsAdapter] Using proxy: %s", proxy)

        self._ws = await websockets.connect(url, **connect_kwargs)

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

def create_ws_adapter(
    use_fingerprint: bool = False,
    impersonate: str = "chrome124",
    version: int = 2
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
        if sys.version_info < (3, 10):
            raise RuntimeError(
                f"TLS fingerprinting requires Python 3.7+, current: {sys.version_info.major}.{sys.version_info.minor}\n"
                "Please upgrade Python or disable fingerprinting (use_fingerprint=False)"
            )

        # 检查 curl_cffi 是否可用
        try:
            import curl_cffi
            from .ws_adapter_threaded import ThreadedCurlWebSocket
            from wsocks.client.ws_adapter_threaded_v2 import ThreadedCurlWebSocketV2   # v2
            from wsocks.client.ws_adapter_cycletls import CycleTLSWebSocket  # v3
            if version == 1:
                logger.info(f"Using ThreadedCurlWebSocket (version: {version})")
                return ThreadedCurlWebSocket(impersonate=impersonate)
            elif  version == 2:
                logger.info(f"Using ThreadedCurlWebSocketV2 (version: {version})")
                return ThreadedCurlWebSocketV2(impersonate=impersonate)
            elif version == 3:
                logger.info(f"Using CycleTLSWebSocket (version: {version})")
                return CycleTLSWebSocket(impersonate=impersonate)
            else:
                logger.warning(f"Invalid version: {version}, use default version 1 ")
                return ThreadedCurlWebSocket(impersonate=impersonate)
        except ImportError:
            raise ImportError(
                "curl_cffi is not installed. Install it with: pip install curl_cffi\n"
                "Or disable fingerprinting (use_fingerprint=False)"
            )
    else:
        logger.info("Using WebSocketsAdapter (standard)")
        return WebSocketsAdapter()

