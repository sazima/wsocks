import asyncio
from typing import Dict
import tornado.websocket
from common.protocol import Protocol, MSG_TYPE_CONNECT, MSG_TYPE_DATA, MSG_TYPE_CLOSE, MSG_TYPE_CONNECT_SUCCESS, MSG_TYPE_CONNECT_FAILED
from common.logger import setup_logger
from server.tcp_client import TargetConnection

logger = setup_logger()

class WebSocketHandler(tornado.websocket.WebSocketHandler):
    """WebSocket 处理器"""

    def initialize(self, password: str, timeout: float = 30.0, max_connections: int = 1000, buffer_size: int = 65536):
        self.password = password
        self.timeout = timeout
        self.max_connections = max_connections
        self.buffer_size = buffer_size
        self.connections: Dict[bytes, TargetConnection] = {}

    def check_origin(self, origin):
        return True

    def open(self):
        logger.info("Client connected")

    async def on_message(self, raw_data: bytes):
        """处理消息"""
        try:
            msg = Protocol.unpack(raw_data, self.password)
            msg_type = msg['type']
            conn_id = msg['conn_id']
            data = msg['data']

            # 使用 ensure_future 异步处理，避免阻塞后续消息 (兼容 Python 3.6+)
            if msg_type == MSG_TYPE_CONNECT:
                # 连接请求
                asyncio.ensure_future(self.handle_connect(conn_id, data))
            elif msg_type == MSG_TYPE_DATA:
                # 数据传输
                asyncio.ensure_future(self.handle_data(conn_id, data))
            elif msg_type == MSG_TYPE_CLOSE:
                # 关闭连接
                asyncio.ensure_future(self.handle_close(conn_id))

        except ValueError as e:
            logger.error(f"Invalid message: {e}")
            self.close()
        except Exception as e:
            logger.error(f"Handle message error: {e}")

    async def handle_connect(self, conn_id: bytes, data: bytes):
        """处理连接请求"""
        try:
            # 检查连接数限制
            if len(self.connections) >= self.max_connections:
                logger.warning(f"[{conn_id.hex()}] Connection limit reached ({self.max_connections})")
                await self.send_connect_failed(conn_id, "Connection limit reached")
                return

            # 解析目标地址
            import ast
            connect_info = ast.literal_eval(data.decode())
            host = connect_info['host']
            port = connect_info['port']

            logger.info(f"[{conn_id.hex()}] Connect request: {host}:{port}")

            # 创建到目标的连接（不占用信号量，允许并行连接）
            connection = TargetConnection(conn_id, host, port, self, self.timeout, self.buffer_size)
            await connection.connect()

            # 再次检查限制（防止并发时超限）
            if len(self.connections) >= self.max_connections:
                logger.warning(f"[{conn_id.hex()}] Connection limit reached after connect")
                await connection.close()
                await self.send_connect_failed(conn_id, "Connection limit reached")
                return

            self.connections[conn_id] = connection

            # 发送连接成功响应
            await self.send_connect_success(conn_id)

        except Exception as e:
            logger.error(f"[{conn_id.hex()}] Connect error: {e}")
            # 发送连接失败响应
            await self.send_connect_failed(conn_id, str(e))

    async def handle_data(self, conn_id: bytes, data: bytes):
        """处理数据"""
        connection = self.connections.get(conn_id)
        if connection:
            await connection.send_data(data)
        else:
            logger.warning(f"[{conn_id.hex()}] Connection not found")

    async def handle_close(self, conn_id: bytes):
        """处理关闭请求"""
        connection = self.connections.pop(conn_id, None)
        if connection:
            await connection.close()

    async def send_data(self, conn_id: bytes, data: bytes):
        """发送数据到客户端"""
        packed_data = Protocol.pack(MSG_TYPE_DATA, conn_id, data, self.password)
        await self.write_message(packed_data, binary=True)

    async def send_connect_success(self, conn_id: bytes):
        """发送连接成功响应"""
        try:
            packed_data = Protocol.pack(MSG_TYPE_CONNECT_SUCCESS, conn_id, b'', self.password)
            await self.write_message(packed_data, binary=True)
            logger.debug(f"[{conn_id.hex()}] Sent connect success response")
        except Exception as e:
            logger.error(f"[{conn_id.hex()}] Failed to send connect success: {e}")

    async def send_connect_failed(self, conn_id: bytes, reason: str = ""):
        """发送连接失败响应"""
        try:
            packed_data = Protocol.pack(MSG_TYPE_CONNECT_FAILED, conn_id, reason.encode(), self.password)
            await self.write_message(packed_data, binary=True)
            logger.debug(f"[{conn_id.hex()}] Sent connect failed response: {reason}")
        except Exception as e:
            logger.error(f"[{conn_id.hex()}] Failed to send connect failed: {e}")

    async def close_connection(self, conn_id: bytes):
        """通知客户端关闭连接"""
        try:
            packed_data = Protocol.pack(MSG_TYPE_CLOSE, conn_id, b'', self.password)
            await self.write_message(packed_data, binary=True)
        except Exception as e:
            logger.debug(f"[{conn_id.hex()}] Failed to send close message: {e}")

        # 使用 pop 避免并发删除时的 KeyError
        self.connections.pop(conn_id, None)

    def on_close(self):
        logger.info("Client disconnected")
        # 清理所有连接
        for connection in list(self.connections.values()):
            asyncio.ensure_future(connection.close())
        self.connections.clear()
