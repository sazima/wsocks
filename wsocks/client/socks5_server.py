import logging
import socket
import struct
import asyncio
import os
import time
import re
import traceback

import msgpack
from typing import Dict, Optional, Tuple, Union
from wsocks.common.logger import setup_logger
from wsocks.common.protocol import Protocol, MSG_TYPE_CONNECT, MSG_TYPE_DATA, MSG_TYPE_CLOSE, MSG_TYPE_UDP_ASSOCIATE, MSG_TYPE_UDP_DATA

logger = setup_logger()

# 性能分析开关（可通过环境变量控制）
ENABLE_PERF_LOG = os.getenv('WSOCKS_PERF_LOG', '0') == '1'

# 乐观发送模式开关（默认启用，可通过环境变量禁用）
ENABLE_OPTIMISTIC_SEND = os.getenv('WSOCKS_OPTIMISTIC_SEND', '1') == '1'

class SOCKS5Connection:
    """SOCKS5 连接"""
    def __init__(self, client_socket: socket.socket, conn_id: bytes, ws_client, optimistic_send: bool = True):
        self.client_socket = client_socket
        self.conn_id = conn_id
        self.ws_client = ws_client
        self.running = True
        self.connect_event = asyncio.Event()
        self.connect_success = False
        self.connect_error = None
        self.optimistic_send = optimistic_send  # 是否使用乐观发送（不等待 CONNECT_SUCCESS）
        self._connect_monitor_task = None  # 后台监听连接结果的任务
        self._send_queue = asyncio.Queue(maxsize=512)  # 管道化发送队列（增大以支持高吞吐量上传）
        self._send_task = None  # 管道化发送任务


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

        if cmd not in [1, 3]:  # 支持 CONNECT (1) 和 UDP ASSOCIATE (3)
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

        return cmd, target_addr, target_port

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
        """转发数据（管道化：读取和发送并行）"""
        # 启动管道化发送任务
        self._send_task = asyncio.ensure_future(self._send_loop())

        # 读取循环：从客户端读取数据，放入队列
        loop = asyncio.get_event_loop()

        try:
            while self.running:
                try:
                    # 从本地客户端读取数据（使用 512KB 缓冲区）
                    data = await asyncio.get_event_loop().sock_recv(self.client_socket, 524288)

                    if not data:
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug(f"[{self.conn_id.hex()}] Client closed")
                        break

                    # 管道化：放入队列，由 _send_loop 发送
                    await self._send_queue.put(data)

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
        finally:
            # 通知发送任务停止
            try:
                await self._send_queue.put(None)  # None 作为结束信号
            except:
                pass

    async def _send_loop(self):
        """发送循环（管道化：从队列取出并发送）"""
        try:
            while self.running:
                # 从队列取出数据
                data = await self._send_queue.get()

                # None 是结束信号
                if data is None:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(f"[{self.conn_id.hex()}] Send loop received stop signal")
                    break

                # 发送到服务端
                try:
                    await self.ws_client.send_message(
                        MSG_TYPE_DATA,
                        self.conn_id,
                        data
                    )
                except Exception as e:
                    logger.error(f"[{self.conn_id.hex()}] Send to server error: {e}")
                    break

        except asyncio.CancelledError:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"[{self.conn_id.hex()}] Send loop cancelled")
        except Exception as e:
            logger.error(f"[{self.conn_id.hex()}] Send loop error: {e}")

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

    async def _monitor_connect_result(self, timeout: float = 30.0):
        """后台监听连接结果（用于乐观发送模式）

        如果连接失败，会关闭连接并清理资源。
        如果超时，也会关闭连接。
        """
        try:
            # 等待连接结果
            await asyncio.wait_for(self.connect_event.wait(), timeout=timeout)

            if not self.connect_success:
                # 连接失败，需要关闭
                error_msg = self.connect_error or "Connection failed"
                logger.error(f"[{self.conn_id.hex()}] Optimistic send failed: {error_msg}")
                await self.close(notify_server=False)  # 不再通知服务端，因为服务端已经知道失败了
            else:
                # 连接成功，记录日志
                if ENABLE_PERF_LOG and logger.isEnabledFor(logging.DEBUG):
                    logger.debug(f"[{self.conn_id.hex()}] [PERF] Optimistic send validated: connection succeeded")
        except asyncio.TimeoutError:
            logger.error(f"[{self.conn_id.hex()}] Optimistic send timeout: no response from server")
            await self.close(notify_server=True)

    async def close(self, notify_server: bool = True):
        """关闭连接

        Args:
            notify_server: 是否通知服务端关闭（如果是服务端主动关闭则不需要）
        """
        if not self.running:
            return

        self.running = False
        logger.info(f"[{self.conn_id.hex()}] Closing connection")

        # 标记连接已关闭，队列中的消息将被过滤掉
        for ws in self.ws_client.ws_pool:
            if ws is not None:
                try:
                    await ws.mark_connection_closed(self.conn_id)
                except Exception:
                    pass  # 忽略错误，可能是 websockets 库不支持

        # 取消后台监听任务（如果有）
        if self._connect_monitor_task and not self._connect_monitor_task.done():
            self._connect_monitor_task.cancel()
            try:
                await self._connect_monitor_task
            except asyncio.CancelledError:
                pass

        # 取消发送任务（如果有）
        if self._send_task and not self._send_task.done():
            self._send_task.cancel()
            try:
                await self._send_task
            except asyncio.CancelledError:
                pass

        if notify_server:
            try:
                # 通知服务端关闭
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(f"[{self.conn_id.hex()}] Sending close message to server")
                await self.ws_client.send_message(MSG_TYPE_CLOSE, self.conn_id, b'')
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(f"[{self.conn_id.hex()}] Sending close message to server success")
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


class HTTPConnectConnection:
    """HTTP CONNECT 连接"""
    def __init__(self, client_socket: socket.socket, conn_id: bytes, ws_client, first_byte: bytes):
        self.client_socket = client_socket
        self.conn_id = conn_id
        self.ws_client = ws_client
        self.first_byte = first_byte  # 已读取的第一个字节
        self.running = True
        self.connect_event = asyncio.Event()
        self.connect_success = False
        self.connect_error = None
        self._send_queue = asyncio.Queue(maxsize=512)  # 管道化发送队列（增大以支持高吞吐量上传）
        self._send_task = None  # 管道化发送任务

    async def handle(self):
        """处理 HTTP CONNECT 连接"""
        try:
            # 解析 HTTP CONNECT 请求
            target_addr, target_port = await self.parse_connect_request()

            logger.info(f"[{self.conn_id.hex()}] HTTP CONNECT to {target_addr}:{target_port}")

            # 发送连接请求到服务端
            connect_data = {
                'host': target_addr,
                'port': target_port
            }
            await self.ws_client.send_message(
                MSG_TYPE_CONNECT,
                self.conn_id,
                msgpack.packb(connect_data)
            )

            # 等待服务端连接响应（最多30秒）
            try:
                await asyncio.wait_for(self.connect_event.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                logger.error(f"[{self.conn_id.hex()}] Connect timeout (30s)")
                await self.send_connect_response(success=False)
                return

            # 检查连接结果
            if self.connect_success:
                # 连接成功，回复 HTTP 200
                await self.send_connect_response(success=True)
                # 开始转发数据
                await self.forward_data()
            else:
                # 连接失败，回复错误
                error_msg = self.connect_error or "Connection failed"
                logger.error(f"[{self.conn_id.hex()}] Connect failed: {error_msg}")
                await self.send_connect_response(success=False)

        except Exception as e:
            logger.error(f"[{self.conn_id.hex()}] HTTP CONNECT error: {e}")
        finally:
            await self.close()

    async def parse_connect_request(self):
        """解析 HTTP CONNECT 请求"""
        # 读取完整的请求行（包括已读的第一个字节）
        request_line = self.first_byte

        # 继续读取直到 \r\n
        while b'\r\n' not in request_line:
            chunk = await asyncio.get_event_loop().sock_recv(self.client_socket, 1024)
            if not chunk:
                raise Exception("Connection closed while reading HTTP request")
            request_line += chunk
            if len(request_line) > 8192:  # 防止恶意请求
                raise Exception("HTTP request line too long")

        # 提取第一行
        first_line = request_line.split(b'\r\n')[0].decode('utf-8')

        # 解析 CONNECT 请求: CONNECT host:port HTTP/1.1
        match = re.match(r'^CONNECT\s+([^:]+):(\d+)\s+HTTP/\d\.\d$', first_line, re.IGNORECASE)
        if not match:
            raise Exception(f"Invalid CONNECT request: {first_line}")

        target_addr = match.group(1)
        target_port = int(match.group(2))

        # 读取并丢弃剩余的 HTTP 头部（直到空行）
        while b'\r\n\r\n' not in request_line:
            chunk = await asyncio.get_event_loop().sock_recv(self.client_socket, 1024)
            if not chunk:
                break
            request_line += chunk
            if len(request_line) > 32768:  # 防止恶意请求
                break

        return target_addr, target_port

    async def send_connect_response(self, success: bool):
        """发送 HTTP CONNECT 响应"""
        if success:
            response = b"HTTP/1.1 200 Connection Established\r\n\r\n"
        else:
            response = b"HTTP/1.1 502 Bad Gateway\r\n\r\n"

        try:
            await asyncio.get_event_loop().sock_sendall(self.client_socket, response)
        except Exception as e:
            logger.error(f"[{self.conn_id.hex()}] Failed to send HTTP response: {e}")

    async def forward_data(self):
        """转发数据（管道化：读取和发送并行）"""
        # 启动管道化发送任务
        self._send_task = asyncio.ensure_future(self._send_loop())

        # 读取循环：从客户端读取数据，放入队列
        loop = asyncio.get_event_loop()

        try:
            while self.running:
                try:
                    # 从本地客户端读取数据（使用 512KB 缓冲区）
                    data = await asyncio.get_event_loop().sock_recv(self.client_socket, 524288)

                    if not data:
                        logger.info(f"[{self.conn_id.hex()}] Client closed")
                        break

                    # 管道化：放入队列，由 _send_loop 发送
                    await self._send_queue.put(data)

                except asyncio.CancelledError:
                    logger.debug(f"[{self.conn_id.hex()}] Forward task cancelled")
                    break
                except OSError as e:
                    if e.errno == 9:  # Bad file descriptor
                        logger.debug(f"[{self.conn_id.hex()}] Socket already closed")
                    else:
                        logger.error(f"[{self.conn_id.hex()}] Forward error: {e}")
                    break
                except Exception as e:
                    logger.error(f"[{self.conn_id.hex()}] Forward error: {e}")
                    break
        finally:
            # 通知发送任务停止
            try:
                await self._send_queue.put(None)
            except:
                pass

    async def _send_loop(self):
        """发送循环（管道化：从队列取出并发送）"""
        try:
            while self.running:
                # 从队列取出数据
                data = await self._send_queue.get()

                # None 是结束信号
                if data is None:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(f"[{self.conn_id.hex()}] Send loop received stop signal")
                    break

                # 发送到服务端
                try:
                    await self.ws_client.send_message(
                        MSG_TYPE_DATA,
                        self.conn_id,
                        data
                    )
                except Exception as e:
                    logger.error(f"[{self.conn_id.hex()}] Send to server error: {e}")
                    break

        except asyncio.CancelledError:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"[{self.conn_id.hex()}] Send loop cancelled")
        except Exception as e:
            logger.error(f"[{self.conn_id.hex()}] Send loop error: {e}")

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
        """关闭连接"""
        if not self.running:
            return

        self.running = False
        logger.info(f"[{self.conn_id.hex()}] Closing connection")

        # 标记连接已关闭，队列中的消息将被过滤掉
        for ws in self.ws_client.ws_pool:
            if ws is not None:
                try:
                    await ws.mark_connection_closed(self.conn_id)
                except Exception:
                    pass  # 忽略错误，可能是 websockets 库不支持

        # 取消发送任务（如果有）
        if self._send_task and not self._send_task.done():
            self._send_task.cancel()
            try:
                await self._send_task
            except asyncio.CancelledError:
                pass

        if notify_server:
            try:
                await self.ws_client.send_message(MSG_TYPE_CLOSE, self.conn_id, b'')
            except Exception as e:
                logger.debug(f"[{self.conn_id.hex()}] Failed to send close message: {e}")

        try:
            self.client_socket.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass

        await asyncio.sleep(0.05)

        try:
            self.client_socket.close()
        except Exception as e:
            logger.debug(f"[{self.conn_id.hex()}] Failed to close socket: {e}")


class UDPRelayServer:
    """UDP Relay 服务器 - 处理 SOCKS5 UDP Associate"""
    def __init__(self, ws_client, udp_timeout: float = 60):
        self.ws_client = ws_client
        self.udp_timeout = udp_timeout
        self.udp_socket: Optional[socket.socket] = None
        self.udp_port = 0
        self.udp_sessions: Dict[bytes, Tuple[str, int, float]] = {}  # conn_id -> (client_addr, client_port, last_activity)
        self.running = False

    async def start(self):
        """启动 UDP relay 服务器"""
        # 创建 UDP socket
        self.udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_socket.bind(('127.0.0.1', 0))  # 绑定到随机端口
        # 保持阻塞模式，以便在 executor 中正常工作
        # self.udp_socket.setblocking(False)
        self.udp_port = self.udp_socket.getsockname()[1]

        logger.info(f"UDP Relay server listening on 127.0.0.1:{self.udp_port}")

        self.running = True

        # 启动接收循环
        asyncio.ensure_future(self._receive_loop())

        # 启动清理超时会话的任务
        asyncio.ensure_future(self._cleanup_sessions())

    def _blocking_recvfrom(self, bufsize):
        """在阻塞模式下接收 UDP 数据（用于 executor）"""
        try:
            return self.udp_socket.recvfrom(bufsize)
        except Exception:
            # 如果 socket 已关闭或出错，返回空数据
            return b'', ('', 0)

    def _blocking_sendto(self, data, addr):
        """在阻塞模式下发送 UDP 数据（用于 executor）"""
        # 创建数据副本，避免在 executor 中使用时被修改
        data_copy = bytes(data)
        try:
            return self.udp_socket.sendto(data_copy, addr)
        except Exception:
            return 0

    async def _receive_loop(self):
        """接收 UDP 数据包"""
        loop = asyncio.get_event_loop()

        while self.running:
            try:
                # 接收数据包（最大 64KB）
                # 使用 run_in_executor 兼容 uvloop（uvloop 不支持 sock_recvfrom）
                print('start recv')
                data, addr = await loop.run_in_executor(None, self._blocking_recvfrom, 65535)
                print('end recv')

                # 解析 SOCKS5 UDP 头
                # 格式: RSV(2) | FRAG(1) | ATYP(1) | DST.ADDR | DST.PORT | DATA
                if len(data) < 10:
                    logger.warning(f"[UDP] Packet too short from {addr}")
                    continue

                rsv, frag, atyp = struct.unpack('!HBB', data[:4])

                if frag != 0:
                    logger.warning(f"[UDP] Fragmentation not supported from {addr}")
                    continue

                # 解析目标地址
                offset = 4
                if atyp == 1:  # IPv4
                    dst_addr = socket.inet_ntoa(data[offset:offset+4])
                    offset += 4
                elif atyp == 3:  # 域名
                    addr_len = data[offset]
                    offset += 1
                    dst_addr = data[offset:offset+addr_len].decode('utf-8')
                    offset += addr_len
                elif atyp == 4:  # IPv6
                    dst_addr = socket.inet_ntop(socket.AF_INET6, data[offset:offset+16])
                    offset += 16
                else:
                    logger.warning(f"[UDP] Unsupported address type {atyp} from {addr}")
                    continue

                # 解析目标端口
                dst_port = struct.unpack('!H', data[offset:offset+2])[0]
                offset += 2

                # 提取实际数据
                payload = data[offset:]

                logger.debug(f"[UDP] {addr} -> {dst_addr}:{dst_port} ({len(payload)} bytes)")

                # 查找或创建会话
                conn_id = self._find_or_create_session(addr)

                # 通过 WebSocket 发送到服务端
                udp_packet = {
                    'dst_addr': dst_addr,
                    'dst_port': dst_port,
                    'data': payload.hex()  # 转为 hex 字符串
                }
                await self.ws_client.send_message(
                    MSG_TYPE_UDP_DATA,
                    conn_id,
                    msgpack.packb(udp_packet)
                )

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(traceback.format_exc())
                logger.error(f"[UDP] Receive error: {e}")
                await asyncio.sleep(0.1)

    def _find_or_create_session(self, addr: Tuple[str, int]) -> bytes:
        """查找或创建 UDP 会话"""
        client_addr, client_port = addr

        # 查找现有会话
        for conn_id, (c_addr, c_port, _) in self.udp_sessions.items():
            if c_addr == client_addr and c_port == client_port:
                # 更新活动时间
                self.udp_sessions[conn_id] = (client_addr, client_port, time.time())
                return conn_id

        # 创建新会话
        conn_id = os.urandom(4)
        self.udp_sessions[conn_id] = (client_addr, client_port, time.time())
        logger.info(f"[UDP] New session {conn_id.hex()} for {client_addr}:{client_port}")
        return conn_id

    async def send_to_client(self, conn_id: bytes, dst_addr: str, dst_port: int, data: bytes):
        """发送数据到客户端"""
        logger.info(f"[UDP] send_to_client called: conn_id={conn_id.hex()}, dst={dst_addr}:{dst_port}, data_size={len(data)}")

        session = self.udp_sessions.get(conn_id)
        if not session:
            logger.warning(f"[UDP] Session {conn_id.hex()} not found in sessions")
            return

        client_addr, client_port, _ = session
        logger.info(f"[UDP] Found session for {conn_id.hex()}: client={client_addr}:{client_port}")

        # 构造 SOCKS5 UDP 响应
        # RSV(2) | FRAG(1) | ATYP(1) | DST.ADDR | DST.PORT | DATA
        packet = struct.pack('!HBB', 0, 0, 1)  # RSV=0, FRAG=0, ATYP=IPv4

        # 添加目标地址（使用发送方地址）
        try:
            packet += socket.inet_aton(dst_addr)
        except:
            # 如果是域名，转为 ATYP=3
            packet = struct.pack('!HBB', 0, 0, 3)
            packet += struct.pack('!B', len(dst_addr))
            packet += dst_addr.encode()

        packet += struct.pack('!H', dst_port)
        packet += data

        logger.info(f"[UDP] Constructed packet size={len(packet)} bytes, sending to {client_addr}:{client_port}")

        # 发送到客户端
        try:
            # 使用 run_in_executor 兼容 uvloop（uvloop 不支持 sock_sendto）
            sent_bytes = await asyncio.get_event_loop().run_in_executor(
                None,
                self._blocking_sendto,
                packet,
                (client_addr, client_port)
            )
            logger.info(f"[UDP] Successfully sent {sent_bytes} bytes (payload: {len(data)} bytes) to {client_addr}:{client_port}")
        except Exception as e:
            logger.error(f"[UDP] Send error to {client_addr}:{client_port}: {e}")
            import traceback
            logger.error(f"[UDP] Send error traceback: {traceback.format_exc()}")

    async def _cleanup_sessions(self):
        """清理超时的 UDP 会话"""
        while self.running:
            try:
                await asyncio.sleep(10)

                now = time.time()
                expired = []

                for conn_id, (addr, port, last_activity) in self.udp_sessions.items():
                    if now - last_activity > self.udp_timeout:
                        expired.append(conn_id)

                for conn_id in expired:
                    logger.info(f"[UDP] Session {conn_id.hex()} expired")
                    del self.udp_sessions[conn_id]

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[UDP] Cleanup error: {e}")

    async def stop(self):
        """停止 UDP relay 服务器"""
        self.running = False
        if self.udp_socket:
            self.udp_socket.close()


class SOCKS5Server:
    """SOCKS5 服务器"""
    def __init__(self, host: str, port: int, ws_client, udp_enabled: bool = False, udp_timeout: float = 60):
        self.host = host
        self.port = port
        self.ws_client = ws_client
        self.connections: Dict[bytes, Union[SOCKS5Connection, HTTPConnectConnection]] = {}
        self.udp_enabled = udp_enabled
        self.udp_relay: Optional[UDPRelayServer] = None

        # 如果启用 UDP，创建 UDP relay 服务器
        if udp_enabled:
            self.udp_relay = UDPRelayServer(ws_client, udp_timeout)

    async def start(self):
        """启动服务器"""
        # 如果启用 UDP，先启动 UDP relay
        if self.udp_enabled and self.udp_relay:
            await self.udp_relay.start()

        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            server_socket.bind((self.host, self.port))
        except OSError as e:
            if e.errno == 98:  # Address already in use
                logger.error(f"Failed to start SOCKS5 server: Port {self.port} is already in use")
                logger.error(f"Please check if another program is using port {self.port}, or change the port in config")
                raise Exception(f"Port {self.port} already in use") from e
            else:
                raise

        server_socket.listen(100)
        server_socket.setblocking(False)

        optimistic_status = "enabled" if ENABLE_OPTIMISTIC_SEND else "disabled"
        logger.info(f"SOCKS5 server listening on {self.host}:{self.port} (optimistic send: {optimistic_status})")

        loop = asyncio.get_event_loop()

        while True:
            client_socket, addr = await loop.sock_accept(server_socket)
            logger.info(f"New connection from {addr}")

            # 生成连接 ID
            conn_id = os.urandom(4)

            # 异步处理连接（带协议自动检测）
            asyncio.ensure_future(self._handle_connection(client_socket, conn_id, addr))

    async def _handle_connection(self, client_socket: socket.socket, conn_id: bytes, addr):
        """处理连接（带协议自动检测）"""
        connection = None
        try:
            # 启用 TCP_NODELAY 以减少延迟
            try:
                client_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except Exception as e:
                logger.debug(f"[{conn_id.hex()}] Failed to set TCP_NODELAY: {e}")

            # 读取第一个字节以检测协议类型
            first_byte = await asyncio.get_event_loop().sock_recv(client_socket, 1)

            if not first_byte:
                logger.warning(f"[{conn_id.hex()}] Connection closed immediately")
                client_socket.close()
                return

            protocol_byte = first_byte[0]

            # 协议检测
            if protocol_byte == 0x05:
                # SOCKS5 协议
                logger.debug(f"[{conn_id.hex()}] Detected SOCKS5 protocol")
                connection = SOCKS5Connection(client_socket, conn_id, self.ws_client, optimistic_send=ENABLE_OPTIMISTIC_SEND)
                self.connections[conn_id] = connection

                # 处理 SOCKS5 握手（第一个字节已经读取）
                await self._handle_socks5_connection(connection, first_byte)

            elif protocol_byte == 0x43:  # 'C' - 可能是 "CONNECT"
                # HTTP CONNECT 协议
                logger.debug(f"[{conn_id.hex()}] Detected HTTP CONNECT protocol")
                connection = HTTPConnectConnection(client_socket, conn_id, self.ws_client, first_byte)
                self.connections[conn_id] = connection

                # 处理 HTTP CONNECT
                await connection.handle()

            else:
                # 未知协议
                logger.warning(f"[{conn_id.hex()}] Unknown protocol (first byte: {protocol_byte:#x})")
                try:
                    client_socket.close()
                except:
                    pass
                return

        except asyncio.CancelledError:
            logger.debug(f"[{conn_id.hex()}] Task cancelled")
            raise
        except Exception as e:
            logger.error(f"[{conn_id.hex()}] Unexpected error: {e}")
            # 确保 socket 被关闭
            try:
                client_socket.close()
            except:
                pass
        finally:
            # 延迟一点删除连接，给服务端发送关闭消息的时间
            await asyncio.sleep(0.1)
            if conn_id in self.connections:
                del self.connections[conn_id]

    async def _handle_socks5_connection(self, connection: SOCKS5Connection, first_byte: bytes):
        """处理 SOCKS5 连接"""
        try:
            # 性能分析：记录总体开始时间
            perf_start_total = time.time() if ENABLE_PERF_LOG else 0

            # 继续 SOCKS5 握手（第一个字节已经读取是 0x05）
            perf_t1 = time.time() if ENABLE_PERF_LOG else 0
            data = first_byte + await asyncio.get_event_loop().sock_recv(connection.client_socket, 1)
            version, nmethods = struct.unpack('!BB', data)

            # 读取认证方法
            methods = await asyncio.get_event_loop().sock_recv(connection.client_socket, nmethods)

            # 回复：无需认证
            response = struct.pack('!BB', 5, 0)
            await asyncio.get_event_loop().sock_sendall(connection.client_socket, response)
            if ENABLE_PERF_LOG and logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"[{connection.conn_id.hex()}] [PERF] SOCKS5 handshake: {(time.time()-perf_t1)*1000:.1f}ms")

            # 解析请求
            perf_t2 = time.time() if ENABLE_PERF_LOG else 0
            cmd, target_addr, target_port = await connection.socks5_connect_request_parse()
            if ENABLE_PERF_LOG and logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"[{connection.conn_id.hex()}] [PERF] Parse connect request: {(time.time()-perf_t2)*1000:.1f}ms")

            if cmd == 3:  # UDP ASSOCIATE
                await self._handle_udp_associate(connection, target_addr, target_port)
            else:  # CONNECT
                # TCP CONNECT
                logger.info(f"[{connection.conn_id.hex()}] Connecting to {target_addr}:{target_port}")

                # 发送连接请求到服务端
                perf_t3 = time.time() if ENABLE_PERF_LOG else 0
                connect_data = {
                    'host': target_addr,
                    'port': target_port
                }
                await connection.ws_client.send_message(
                    MSG_TYPE_CONNECT,
                    connection.conn_id,
                    msgpack.packb(connect_data)
                )
                if ENABLE_PERF_LOG and logger.isEnabledFor(logging.DEBUG):
                    logger.debug(f"[{connection.conn_id.hex()}] [PERF] Send CONNECT to server: {(time.time()-perf_t3)*1000:.1f}ms")

                # 乐观发送模式：不等待服务端响应，立即回复 SOCKS5 成功
                if connection.optimistic_send:
                    perf_t5 = time.time() if ENABLE_PERF_LOG else 0
                    # 立即回复 SOCKS5 客户端（乐观假设连接会成功）
                    await connection.socks5_connect_response(success=True)
                    if ENABLE_PERF_LOG and logger.isEnabledFor(logging.DEBUG):
                        logger.debug(f"[{connection.conn_id.hex()}] [PERF] Send SOCKS5 response (optimistic): {(time.time()-perf_t5)*1000:.1f}ms")
                        logger.debug(f"[{connection.conn_id.hex()}] [PERF] *** TOTAL connection setup (optimistic): {(time.time()-perf_start_total)*1000:.1f}ms *** ⚡")

                    # 启动后台任务监听实际的连接结果
                    connection._connect_monitor_task = asyncio.ensure_future(
                        connection._monitor_connect_result(timeout=30.0)
                    )

                    # 立即开始转发数据（不等待服务端确认）
                    await connection.forward_data()

                else:
                    # 传统模式：等待服务端连接响应（最多30秒）
                    perf_t4 = time.time() if ENABLE_PERF_LOG else 0
                    try:
                        await asyncio.wait_for(connection.connect_event.wait(), timeout=30.0)
                    except asyncio.TimeoutError:
                        logger.error(f"[{connection.conn_id.hex()}] Connect timeout (30s)")
                        await connection.socks5_connect_response(success=False, error_msg="Connection timeout")
                        return

                    if ENABLE_PERF_LOG and logger.isEnabledFor(logging.DEBUG):
                        logger.debug(f"[{connection.conn_id.hex()}] [PERF] Wait server response: {(time.time()-perf_t4)*1000:.1f}ms ⚡")

                    # 检查连接结果
                    if connection.connect_success:
                        # 连接成功，回复 SOCKS5 客户端
                        perf_t5 = time.time() if ENABLE_PERF_LOG else 0
                        await connection.socks5_connect_response(success=True)
                        if ENABLE_PERF_LOG and logger.isEnabledFor(logging.DEBUG):
                            logger.debug(f"[{connection.conn_id.hex()}] [PERF] Send SOCKS5 response: {(time.time()-perf_t5)*1000:.1f}ms")
                            logger.debug(f"[{connection.conn_id.hex()}] [PERF] *** TOTAL connection setup: {(time.time()-perf_start_total)*1000:.1f}ms ***")
                        # 开始转发数据
                        await connection.forward_data()
                    else:
                        # 连接失败，回复错误
                        error_msg = connection.connect_error or "Connection failed"
                        logger.error(f"[{connection.conn_id.hex()}] Connect failed: {error_msg}")
                        await connection.socks5_connect_response(success=False, error_msg=error_msg)

        except Exception as e:
            logger.error(f"[{connection.conn_id.hex()}] SOCKS5 error: {e}")
            raise
        finally:
            # 确保连接被正确关闭并通知服务端
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"[{connection.conn_id.hex()}] start close SOCKS5 connection")
            await connection.close()
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"[{connection.conn_id.hex()}] SOCKS5 connection closed")

    async def _handle_udp_associate(self, connection: SOCKS5Connection, target_addr: str, target_port: int):
        """处理 UDP ASSOCIATE 请求"""
        if not self.udp_enabled or not self.udp_relay:
            logger.warning(f"[{connection.conn_id.hex()}] UDP ASSOCIATE requested but UDP is disabled")
            await connection.socks5_connect_response(success=False, error_msg="UDP not supported")
            return

        logger.info(f"[{connection.conn_id.hex()}] UDP ASSOCIATE request")

        # 回复 UDP relay 地址
        # 格式: VER(1) | REP(1) | RSV(1) | ATYP(1) | BND.ADDR | BND.PORT
        response = struct.pack('!BBBB', 5, 0, 0, 1)  # 成功
        response += socket.inet_aton('127.0.0.1')  # UDP relay 地址
        response += struct.pack('!H', self.udp_relay.udp_port)  # UDP relay 端口

        try:
            await asyncio.get_event_loop().sock_sendall(connection.client_socket, response)
            logger.info(f"[{connection.conn_id.hex()}] UDP relay available at 127.0.0.1:{self.udp_relay.udp_port}")
        except Exception as e:
            logger.error(f"[{connection.conn_id.hex()}] Failed to send UDP ASSOCIATE response: {e}")
            return

        # 保持 TCP 连接活跃（当 TCP 连接关闭时，UDP 会话也应该结束）
        # 这里简单地等待连接关闭
        try:
            while True:
                data = await asyncio.get_event_loop().sock_recv(connection.client_socket, 1024)
                if not data:
                    logger.info(f"[{connection.conn_id.hex()}] UDP ASSOCIATE TCP connection closed")
                    break
                await asyncio.sleep(0.1)
        except Exception as e:
            logger.debug(f"[{connection.conn_id.hex()}] UDP ASSOCIATE connection error: {e}")

    def get_connection(self, conn_id: bytes) -> Optional[Union[SOCKS5Connection, HTTPConnectConnection]]:
        """获取连接（支持 SOCKS5 和 HTTP CONNECT）"""
        return self.connections.get(conn_id)

    def get_udp_relay(self) -> Optional[UDPRelayServer]:
        """获取 UDP relay 服务器"""
        return self.udp_relay
