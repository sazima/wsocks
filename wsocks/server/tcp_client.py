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
        self._read_task: Optional[asyncio.Task] = None
        self._closing = False  # 防止并发关闭

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

            # 开始读取数据，保存任务引用以便后续取消
            self._read_task = asyncio.ensure_future(self.read_loop())

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

        except asyncio.CancelledError:
            # 任务被取消，这是正常的关闭流程
            logger.debug(f"[{self.conn_id.hex()}] Read loop cancelled")
            raise  # 重新抛出 CancelledError，确保任务正确结束
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
        # 防止并发关闭：只允许第一个 close() 调用执行完整的清理流程
        if self._closing:
            logger.debug(f"[{self.conn_id.hex()}] Already in closing process, skipping")
            return

        self._closing = True

        if not self.running:
            logger.debug(f"[{self.conn_id.hex()}] Already closed (running=False)")
            return

        self.running = False
        logger.info(f"[{self.conn_id.hex()}] Closing target connection")

        try:
            # 立即取消 read_loop 任务，避免阻塞在读取上（必须先做，确保数据不再发送）
            if self._read_task:
                logger.debug(f"[{self.conn_id.hex()}] Read task status: done={self._read_task.done()}, cancelled={self._read_task.cancelled()}")
                if not self._read_task.done():
                    logger.info(f"[{self.conn_id.hex()}] Cancelling read task")
                    self._read_task.cancel()
                    try:
                        await self._read_task
                        logger.debug(f"[{self.conn_id.hex()}] Read task await completed")
                    except asyncio.CancelledError:
                        logger.debug(f"[{self.conn_id.hex()}] Read task cancelled")
                    except Exception as e:
                        logger.debug(f"[{self.conn_id.hex()}] Read task error: {e}")
            else:
                logger.warning(f"[{self.conn_id.hex()}] No read task found!")

            # 关闭写入端
            if self.writer:
                try:
                    self.writer.close()
                    # wait_closed() 是 Python 3.7+ 才有的方法
                    if hasattr(self.writer, 'wait_closed'):
                        await self.writer.wait_closed()
                except Exception as e:
                    logger.debug(f"[{self.conn_id.hex()}] Writer close error: {e}")

        finally:
            # 无论如何都要通知客户端关闭（即使前面出错）
            try:
                await self.ws_handler.close_connection(self.conn_id)
            except Exception as e:
                logger.debug(f"[{self.conn_id.hex()}] Failed to notify client close: {e}")
