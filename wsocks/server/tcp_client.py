import asyncio
import socket
from typing import Optional
from wsocks.common.logger import setup_logger

logger = setup_logger()

class TargetConnection:
    """目标服务器连接"""
    def __init__(self, conn_id: bytes, host: str, port: int, ws_handler, timeout: float = 30.0, buffer_size: int = 65536):
        self.conn_id = conn_id
        self.host = host
        self.port = port
        self.ws_handler = ws_handler
        self.timeout = timeout
        self.buffer_size = buffer_size
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.running = False

    async def connect(self):
        """连接到目标服务器"""
        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.timeout
            )

            # 启用 TCP_NODELAY 以减少延迟
            sock = self.writer.get_extra_info('socket')
            if sock:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            self.running = True
            logger.info(f"[{self.conn_id.hex()}] Connected to {self.host}:{self.port}")

            # 开始读取数据
            asyncio.ensure_future(self.read_loop())

        except asyncio.TimeoutError:
            logger.error(f"[{self.conn_id.hex()}] Connect timeout: {self.host}:{self.port}")
            raise
        except Exception as e:
            logger.error(f"[{self.conn_id.hex()}] Connect error: {e}")
            raise

    async def read_loop(self):
        """读取数据循环"""
        try:
            while self.running:
                # 移除读取超时，使用更大的缓冲区提升性能
                data = await self.reader.read(self.buffer_size)

                if not data:
                    logger.info(f"[{self.conn_id.hex()}] Target closed")
                    break

                # 发送回客户端
                await self.ws_handler.send_data(self.conn_id, data)

        except Exception as e:
            logger.error(f"[{self.conn_id.hex()}] Read error: {e}")
        finally:
            await self.close()

    async def send_data(self, data: bytes):
        """发送数据到目标服务器"""
        if not self.running or not self.writer:
            return

        try:
            self.writer.write(data)
            await asyncio.wait_for(self.writer.drain(), timeout=self.timeout)
        except asyncio.TimeoutError:
            logger.error(f"[{self.conn_id.hex()}] Send timeout")
            await self.close()
        except Exception as e:
            logger.error(f"[{self.conn_id.hex()}] Send error: {e}")
            await self.close()

    async def close(self):
        """关闭连接"""
        if not self.running:
            return

        self.running = False
        logger.info(f"[{self.conn_id.hex()}] Closing target connection")

        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception as e:
                logger.debug(f"[{self.conn_id.hex()}] Writer close error: {e}")

        # 通知客户端关闭
        await self.ws_handler.close_connection(self.conn_id)
