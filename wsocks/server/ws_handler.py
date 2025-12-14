import asyncio
import socket
import time
from typing import Dict, Tuple, Optional
import tornado.websocket
from wsocks.common.protocol import Protocol, MSG_TYPE_CONNECT, MSG_TYPE_DATA, MSG_TYPE_CLOSE, MSG_TYPE_CONNECT_SUCCESS, MSG_TYPE_CONNECT_FAILED, MSG_TYPE_HEARTBEAT, MSG_TYPE_UDP_DATA
from wsocks.common.logger import setup_logger
from wsocks.server.tcp_client import TargetConnection

logger = setup_logger()

class WebSocketHandler(tornado.websocket.WebSocketHandler):
    """WebSocket 处理器"""

    def initialize(self, password: str, timeout: float = 30.0, max_connections: int = 1000, buffer_size: int = 65536):
        self.password = password
        self.timeout = timeout
        self.max_connections = max_connections
        self.buffer_size = buffer_size
        self.connections: Dict[bytes, TargetConnection] = {}
        self.udp_sessions: Dict[bytes, Tuple[socket.socket, str, int, float]] = {}  # conn_id -> (udp_socket, dst_addr, dst_port, last_activity)

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
            elif msg_type == MSG_TYPE_HEARTBEAT:
                # 心跳消息，回应确认（可选）
                logger.debug(f"Received heartbeat ({len(data)} bytes)")
                # 可以选择回应心跳或仅忽略
                # asyncio.ensure_future(self.send_heartbeat_response(conn_id, data))
            elif msg_type == MSG_TYPE_UDP_DATA:
                # UDP 数据消息
                asyncio.ensure_future(self.handle_udp_data(conn_id, data))

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
        logger.info(f"[{conn_id.hex()}] Received CLOSE message from client")
        connection = self.connections.pop(conn_id, None)
        if connection:
            await connection.close()
        else:
            logger.warning(f"[{conn_id.hex()}] Connection not found in handle_close")

    async def send_data(self, conn_id: bytes, data: bytes):
        """发送数据到客户端"""
        try:
            packed_data = Protocol.pack(MSG_TYPE_DATA, conn_id, data, self.password)
            await self.write_message(packed_data, binary=True)
        except Exception as e:
            # WebSocket 已关闭或发送失败，记录并抛出异常让调用方处理
            logger.debug(f"[{conn_id.hex()}] Failed to send data to client: {e}")
            raise

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

    async def handle_udp_data(self, conn_id: bytes, data: bytes):
        """处理 UDP 数据"""
        try:
            # 解析 UDP 数据包
            import ast
            udp_packet = ast.literal_eval(data.decode())
            dst_addr = udp_packet['dst_addr']
            dst_port = udp_packet['dst_port']
            payload = bytes.fromhex(udp_packet['data'])

            logger.debug(f"[UDP-{conn_id.hex()}] -> {dst_addr}:{dst_port} ({len(payload)} bytes)")

            # 查找或创建 UDP socket
            udp_socket = await self._get_or_create_udp_socket(conn_id, dst_addr, dst_port)

            # 发送数据
            await asyncio.get_event_loop().sock_sendto(udp_socket, payload, (dst_addr, dst_port))

            # 更新活动时间
            if conn_id in self.udp_sessions:
                sock, addr, port, _ = self.udp_sessions[conn_id]
                self.udp_sessions[conn_id] = (sock, addr, port, time.time())

        except Exception as e:
            logger.error(f"[UDP-{conn_id.hex()}] Error: {e}")

    async def _get_or_create_udp_socket(self, conn_id: bytes, dst_addr: str, dst_port: int) -> socket.socket:
        """获取或创建 UDP socket"""
        # 检查是否已有会话
        if conn_id in self.udp_sessions:
            udp_socket, _, _, _ = self.udp_sessions[conn_id]
            return udp_socket

        # 创建新的 UDP socket
        udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp_socket.setblocking(False)

        # 保存会话
        self.udp_sessions[conn_id] = (udp_socket, dst_addr, dst_port, time.time())

        logger.info(f"[UDP-{conn_id.hex()}] New UDP session for {dst_addr}:{dst_port}")

        # 启动接收任务
        asyncio.ensure_future(self._udp_receive_loop(conn_id, udp_socket, dst_addr, dst_port))

        return udp_socket

    async def _udp_receive_loop(self, conn_id: bytes, udp_socket: socket.socket, dst_addr: str, dst_port: int):
        """接收 UDP 响应"""
        try:
            while conn_id in self.udp_sessions:
                try:
                    # 接收数据（超时 60 秒）
                    data, addr = await asyncio.wait_for(
                        asyncio.get_event_loop().sock_recvfrom(udp_socket, 65535),
                        timeout=60.0
                    )

                    logger.debug(f"[UDP-{conn_id.hex()}] <- {addr} ({len(data)} bytes)")

                    # 发送回客户端
                    udp_response = {
                        'dst_addr': addr[0],
                        'dst_port': addr[1],
                        'data': data.hex()
                    }
                    packed_data = Protocol.pack(MSG_TYPE_UDP_DATA, conn_id, str(udp_response).encode(), self.password)
                    await self.write_message(packed_data, binary=True)

                    # 更新活动时间
                    if conn_id in self.udp_sessions:
                        sock, dst_a, dst_p, _ = self.udp_sessions[conn_id]
                        self.udp_sessions[conn_id] = (sock, dst_a, dst_p, time.time())

                except asyncio.TimeoutError:
                    # 超时，关闭会话
                    logger.info(f"[UDP-{conn_id.hex()}] Session timeout")
                    break
                except Exception as e:
                    logger.error(f"[UDP-{conn_id.hex()}] Receive error: {e}")
                    break

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[UDP-{conn_id.hex()}] Loop error: {e}")
        finally:
            # 清理会话
            if conn_id in self.udp_sessions:
                udp_socket.close()
                del self.udp_sessions[conn_id]
                logger.info(f"[UDP-{conn_id.hex()}] Session closed")

    def on_close(self):
        logger.info("Client disconnected")
        # 清理所有连接
        for connection in list(self.connections.values()):
            asyncio.ensure_future(connection.close())
        self.connections.clear()

        # 清理所有 UDP 会话
        for conn_id, (udp_socket, _, _, _) in list(self.udp_sessions.items()):
            try:
                udp_socket.close()
            except:
                pass
        self.udp_sessions.clear()
