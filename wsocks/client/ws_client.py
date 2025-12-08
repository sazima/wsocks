import asyncio
import websockets
from typing import List, Optional
from wsocks.common.protocol import Protocol, MSG_TYPE_DATA, MSG_TYPE_CLOSE, MSG_TYPE_CONNECT_SUCCESS, MSG_TYPE_CONNECT_FAILED
from wsocks.common.logger import setup_logger

logger = setup_logger()

class WebSocketClient:
    """WebSocket 客户端（支持连接池）"""
    def __init__(self, url: str, password: str, socks5_server, ping_interval: float = 30, ping_timeout: float = 10, compression: bool = True, pool_size: int = 8):
        self.url = url
        self.password = password
        self.socks5_server = socks5_server
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.compression = 'deflate' if compression else None
        self.pool_size = pool_size
        self.ws_pool: List[Optional[websockets.WebSocketClientProtocol]] = [None] * pool_size
        self.running = False
        self.next_ws_index = 0  # 轮询索引

    async def connect(self):
        """连接到服务器（连接池中的所有连接）"""
        self.running = True

        # 并发创建所有连接
        logger.info(f"Creating WebSocket connection pool (size={self.pool_size})")
        tasks = []
        for i in range(self.pool_size):
            task = asyncio.ensure_future(self._connect_single(i))
            tasks.append(task)

        # 等待所有连接任务启动
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _connect_single(self, index: int):
        """连接单个 WebSocket（带重连）"""
        while self.running:
            try:
                logger.info(f"[WS-{index}] Connecting to {self.url}")
                ws = await websockets.connect(
                    self.url,
                    ping_interval=self.ping_interval,
                    ping_timeout=self.ping_timeout,
                    compression=self.compression
                )
                self.ws_pool[index] = ws
                logger.info(f"[WS-{index}] Connected (compression={self.compression}, ping_interval={self.ping_interval}s)")

                # 开始接收消息
                await self._receive_loop(index, ws)

            except Exception as e:
                logger.error(f"[WS-{index}] Connection error: {e}")
                self.ws_pool[index] = None
                await asyncio.sleep(5)  # 重连延迟

    async def _receive_loop(self, index: int, ws: websockets.WebSocketClientProtocol):
        """接收消息循环（单个连接）"""
        try:
            async for message in ws:
                await self.handle_message(message)
        except websockets.exceptions.ConnectionClosed:
            logger.warning(f"[WS-{index}] Connection closed")
            self.ws_pool[index] = None
        except Exception as e:
            logger.error(f"[WS-{index}] Receive error: {e}")
            self.ws_pool[index] = None

    async def handle_message(self, raw_data: bytes):
        """处理接收到的消息"""
        try:
            msg = Protocol.unpack(raw_data, self.password)
            msg_type = msg['type']
            conn_id = msg['conn_id']
            data = msg['data']

            connection = self.socks5_server.get_connection(conn_id)

            if msg_type == MSG_TYPE_CONNECT_SUCCESS:
                # 连接成功响应
                if connection:
                    connection.on_connect_success()
                else:
                    logger.warning(f"[{conn_id.hex()}] Connection not found for connect success")

            elif msg_type == MSG_TYPE_CONNECT_FAILED:
                # 连接失败响应
                reason = data.decode() if data else "Unknown error"
                if connection:
                    connection.on_connect_failed(reason)
                else:
                    logger.warning(f"[{conn_id.hex()}] Connection not found for connect failed")

            elif msg_type == MSG_TYPE_DATA:
                # 数据消息，转发到本地客户端
                if connection:
                    await connection.send_to_client(data)
                else:
                    logger.warning(f"[{conn_id.hex()}] Connection not found")

            elif msg_type == MSG_TYPE_CLOSE:
                # 关闭连接（不再通知服务端，避免循环）
                if connection:
                    await connection.close(notify_server=False)
                else:
                    # 连接可能已经被清理，这是正常情况
                    logger.debug(f"[{conn_id.hex()}] Connection already closed")

        except Exception as e:
            logger.error(f"Handle message error: {e}")

    async def send_message(self, msg_type: int, conn_id: bytes, data: bytes):
        """发送消息（负载均衡到连接池）"""
        if not self.running:
            raise Exception("Not connected")

        packed_data = Protocol.pack(msg_type, conn_id, data, self.password)

        # 负载均衡策略：基于 conn_id 哈希，确保同一连接的消息通过同一个 WebSocket
        # 这样可以保持消息顺序
        ws_index = int.from_bytes(conn_id, 'big') % self.pool_size

        # 如果该连接不可用，尝试其他连接
        for attempt in range(self.pool_size):
            current_index = (ws_index + attempt) % self.pool_size
            ws = self.ws_pool[current_index]

            if ws is not None:
                try:
                    await ws.send(packed_data)
                    return
                except Exception as e:
                    logger.warning(f"[WS-{current_index}] Send failed: {e}, trying next...")
                    self.ws_pool[current_index] = None
                    continue

        # 所有连接都不可用
        raise Exception("All WebSocket connections unavailable")
