import socket
import struct
import asyncio
import os
from typing import Dict, Optional
from wsocks.common.logger import setup_logger
from wsocks.common.protocol import Protocol, MSG_TYPE_CONNECT, MSG_TYPE_DATA, MSG_TYPE_CLOSE

logger = setup_logger()

class SOCKS5Connection:
    """SOCKS5 连接"""
    def __init__(self, client_socket: socket.socket, conn_id: bytes, ws_client):
        self.client_socket = client_socket
        self.conn_id = conn_id
        self.ws_client = ws_client
        self.running = True
        self.connect_event = asyncio.Event()
        self.connect_success = False
        self.connect_error = None

    async def handle(self):
        """处理连接"""
        try:
            # SOCKS5 握手
            await self.socks5_handshake()

            # SOCKS5 连接请求（读取目标地址，但暂不回复）
            target_addr, target_port = await self.socks5_connect_request_parse()

            logger.info(f"[{self.conn_id.hex()}] Connecting to {target_addr}:{target_port}")

            # 发送连接请求到服务端
            connect_data = {
                'host': target_addr,
                'port': target_port
            }
            await self.ws_client.send_message(
                MSG_TYPE_CONNECT,
                self.conn_id,
                str(connect_data).encode()
            )

            # 等待服务端连接响应（最多30秒）
            try:
                await asyncio.wait_for(self.connect_event.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                logger.error(f"[{self.conn_id.hex()}] Connect timeout (30s)")
                await self.socks5_connect_response(success=False, error_msg="Connection timeout")
                return

            # 检查连接结果
            if self.connect_success:
                # 连接成功，回复 SOCKS5 客户端
                await self.socks5_connect_response(success=True)
                # 开始转发数据
                await self.forward_data()
            else:
                # 连接失败，回复错误
                error_msg = self.connect_error or "Connection failed"
                logger.error(f"[{self.conn_id.hex()}] Connect failed: {error_msg}")
                await self.socks5_connect_response(success=False, error_msg=error_msg)

        except Exception as e:
            logger.error(f"[{self.conn_id.hex()}] Error: {e}")
        finally:
            await self.close()

    async def socks5_handshake(self):
        """SOCKS5 握手"""
        # 读取客户端握手请求
        data = await asyncio.get_event_loop().sock_recv(self.client_socket, 2)
        version, nmethods = struct.unpack('!BB', data)

        if version != 5:
            raise Exception(f"Unsupported SOCKS version: {version}")

        # 读取认证方法
        methods = await asyncio.get_event_loop().sock_recv(self.client_socket, nmethods)

        # 回复：无需认证
        response = struct.pack('!BB', 5, 0)
        await asyncio.get_event_loop().sock_sendall(self.client_socket, response)

    async def socks5_connect_request_parse(self):
        """解析 SOCKS5 连接请求（不回复）"""
        # 读取请求头
        data = await asyncio.get_event_loop().sock_recv(self.client_socket, 4)
        version, cmd, _, atyp = struct.unpack('!BBBB', data)

        if version != 5:
            raise Exception(f"Unsupported SOCKS version: {version}")

        if cmd != 1:  # 只支持 CONNECT
            raise Exception(f"Unsupported command: {cmd}")

        # 读取目标地址
        if atyp == 1:  # IPv4
            addr_data = await asyncio.get_event_loop().sock_recv(self.client_socket, 4)
            target_addr = socket.inet_ntoa(addr_data)
        elif atyp == 3:  # 域名
            addr_len = await asyncio.get_event_loop().sock_recv(self.client_socket, 1)
            addr_len = struct.unpack('!B', addr_len)[0]
            addr_data = await asyncio.get_event_loop().sock_recv(self.client_socket, addr_len)
            target_addr = addr_data.decode('utf-8')
        elif atyp == 4:  # IPv6
            addr_data = await asyncio.get_event_loop().sock_recv(self.client_socket, 16)
            target_addr = socket.inet_ntop(socket.AF_INET6, addr_data)
        else:
            raise Exception(f"Unsupported address type: {atyp}")

        # 读取目标端口
        port_data = await asyncio.get_event_loop().sock_recv(self.client_socket, 2)
        target_port = struct.unpack('!H', port_data)[0]

        return target_addr, target_port

    async def socks5_connect_response(self, success: bool, error_msg: str = ""):
        """回复 SOCKS5 连接结果"""
        if success:
            # 回复连接成功
            response = struct.pack('!BBBB', 5, 0, 0, 1)  # 成功
            response += socket.inet_aton('0.0.0.0')  # 绑定地址
            response += struct.pack('!H', 0)  # 绑定端口
        else:
            # 回复连接失败（错误码 1 = 一般性 SOCKS 服务器故障）
            response = struct.pack('!BBBB', 5, 1, 0, 1)  # 失败
            response += socket.inet_aton('0.0.0.0')  # 绑定地址
            response += struct.pack('!H', 0)  # 绑定端口

        try:
            await asyncio.get_event_loop().sock_sendall(self.client_socket, response)
        except Exception as e:
            logger.error(f"[{self.conn_id.hex()}] Failed to send SOCKS5 response: {e}")

    async def forward_data(self):
        """转发数据"""
        loop = asyncio.get_event_loop()

        while self.running:
            try:
                # 从本地客户端读取数据（使用 64KB 缓冲区，移除超时）
                data = await loop.sock_recv(self.client_socket, 65536)

                if not data:
                    logger.info(f"[{self.conn_id.hex()}] Client closed")
                    break

                # 发送到服务端
                await self.ws_client.send_message(
                    MSG_TYPE_DATA,
                    self.conn_id,
                    data
                )

            except asyncio.CancelledError:
                logger.debug(f"[{self.conn_id.hex()}] Forward task cancelled")
                break
            except OSError as e:
                # Socket 已关闭
                if e.errno == 9:  # Bad file descriptor
                    logger.debug(f"[{self.conn_id.hex()}] Socket already closed")
                else:
                    logger.error(f"[{self.conn_id.hex()}] Forward error: {e}")
                break
            except Exception as e:
                logger.error(f"[{self.conn_id.hex()}] Forward error: {e}")
                break

    async def send_to_client(self, data: bytes):
        """发送数据到本地客户端"""
        try:
            await asyncio.get_event_loop().sock_sendall(self.client_socket, data)
        except Exception as e:
            logger.error(f"[{self.conn_id.hex()}] Send to client error: {e}")
            await self.close()

    def on_connect_success(self):
        """连接成功回调"""
        logger.info(f"[{self.conn_id.hex()}] Server connected successfully")
        self.connect_success = True
        self.connect_event.set()

    def on_connect_failed(self, reason: str):
        """连接失败回调"""
        logger.warning(f"[{self.conn_id.hex()}] Server connect failed: {reason}")
        self.connect_success = False
        self.connect_error = reason
        self.connect_event.set()

    async def close(self, notify_server: bool = True):
        """关闭连接

        Args:
            notify_server: 是否通知服务端关闭（如果是服务端主动关闭则不需要）
        """
        if not self.running:
            return

        self.running = False
        logger.info(f"[{self.conn_id.hex()}] Closing connection")

        if notify_server:
            try:
                # 通知服务端关闭
                await self.ws_client.send_message(MSG_TYPE_CLOSE, self.conn_id, b'')
            except Exception as e:
                logger.debug(f"[{self.conn_id.hex()}] Failed to send close message: {e}")

        # 先关闭 socket 的发送端，让正在读取的操作能够正常结束
        try:
            self.client_socket.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass

        # 等待一小段时间，让 forward_data 中的读取操作正常结束
        await asyncio.sleep(0.05)

        try:
            self.client_socket.close()
        except Exception as e:
            logger.debug(f"[{self.conn_id.hex()}] Failed to close socket: {e}")


class SOCKS5Server:
    """SOCKS5 服务器"""
    def __init__(self, host: str, port: int, ws_client):
        self.host = host
        self.port = port
        self.ws_client = ws_client
        self.connections: Dict[bytes, SOCKS5Connection] = {}

    async def start(self):
        """启动服务器"""
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((self.host, self.port))
        server_socket.listen(100)
        server_socket.setblocking(False)

        logger.info(f"SOCKS5 server listening on {self.host}:{self.port}")

        loop = asyncio.get_event_loop()

        while True:
            client_socket, addr = await loop.sock_accept(server_socket)
            logger.info(f"New connection from {addr}")

            # 生成连接 ID
            conn_id = os.urandom(4)

            # 创建连接处理
            connection = SOCKS5Connection(client_socket, conn_id, self.ws_client)
            self.connections[conn_id] = connection

            # 异步处理连接
            asyncio.ensure_future(self._handle_connection(connection))

    async def _handle_connection(self, connection: SOCKS5Connection):
        """处理连接"""
        try:
            await connection.handle()
        except asyncio.CancelledError:
            logger.debug(f"[{connection.conn_id.hex()}] Task cancelled")
            raise
        except Exception as e:
            logger.error(f"[{connection.conn_id.hex()}] Unexpected error: {e}")
        finally:
            # 延迟一点删除连接，给服务端发送关闭消息的时间
            await asyncio.sleep(0.1)
            if connection.conn_id in self.connections:
                del self.connections[connection.conn_id]

    def get_connection(self, conn_id: bytes) -> Optional[SOCKS5Connection]:
        """获取连接"""
        return self.connections.get(conn_id)
