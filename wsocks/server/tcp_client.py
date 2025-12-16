import asyncio
import logging
import socket
from typing import Optional
from wsocks.common.logger import setup_logger

logger = setup_logger()

class TargetConnection:
    """目标服务器连接"""
    def __init__(self, conn_id: bytes, host: str, port: int, ws_handler, timeout: float = 30.0, buffer_size: int = 1048576):
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
        self._send_task: Optional[asyncio.Task] = None  # 管道化发送任务
        self._closing = False  # 防止并发关闭
        self._early_data_buffer = []  # 缓存在连接建立前到达的数据（乐观发送）
        self._connected = False  # 标记连接是否已建立
        self._send_queue: Optional[asyncio.Queue] = None  # 管道化发送队列

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
            self._connected = True  # 标记连接已建立
            logger.info(f"[{self.conn_id.hex()}] Connected to {self.host}:{self.port}")

            # 创建管道化发送队列（缓冲 8 个数据包）
            self._send_queue = asyncio.Queue(maxsize=8)

            # 发送缓存的早期数据（乐观发送模式）
            if self._early_data_buffer:
                logger.info(f"[{self.conn_id.hex()}] Sending {len(self._early_data_buffer)} buffered early data packets")
                for data in self._early_data_buffer:
                    try:
                        self.writer.write(data)
                        await asyncio.wait_for(self.writer.drain(), timeout=self.timeout)
                    except Exception as e:
                        logger.error(f"[{self.conn_id.hex()}] Failed to send buffered data: {e}")
                        raise
                self._early_data_buffer.clear()

            # 启动管道化读取和发送任务
            self._read_task = asyncio.ensure_future(self.read_loop())
            self._send_task = asyncio.ensure_future(self.send_loop())

        except asyncio.TimeoutError:
            logger.error(f"[{self.conn_id.hex()}] Connect timeout: {self.host}:{self.port}")
            raise
        except Exception as e:
            logger.error(f"[{self.conn_id.hex()}] Connect error: {e}")
            raise

    async def read_loop(self):
        """读取数据循环（管道化：读取后放入队列）"""
        try:
            while self.running:
                # 使用更大的缓冲区（1MB）提升性能
                data = await self.reader.read(self.buffer_size)
                if not self.running:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(f"[{self.conn_id.hex()}] not running break.")
                    break
                if not data:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(f"[{self.conn_id.hex()}] Target closed")
                    break

                # 管道化：放入队列，由 send_loop 发送（非阻塞）
                try:
                    await self._send_queue.put(data)
                except Exception as e:
                    logger.error(f"[{self.conn_id.hex()}] Queue put error: {e}")
                    break

        except asyncio.CancelledError:
            # 任务被取消，这是正常的关闭流程
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"[{self.conn_id.hex()}] Read loop cancelled")
            raise  # 重新抛出 CancelledError，确保任务正确结束
        except Exception as e:
            logger.error(f"[{self.conn_id.hex()}] Read error: {e}")
        finally:
            # 通知 send_loop 停止
            if self._send_queue:
                try:
                    await self._send_queue.put(None)  # None 作为结束信号
                except:
                    pass
            await self.close()

    async def send_loop(self):
        """发送数据循环（管道化：从队列取出并发送）"""
        try:
            while self.running:
                # 从队列取出数据
                data = await self._send_queue.get()

                # None 是结束信号
                if data is None:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(f"[{self.conn_id.hex()}] Send loop received stop signal")
                    break

                # 发送到客户端
                try:
                    await self.ws_handler.send_data(self.conn_id, data)
                except Exception as e:
                    logger.error(f"[{self.conn_id.hex()}] Send to client error: {e}")
                    break

        except asyncio.CancelledError:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"[{self.conn_id.hex()}] Send loop cancelled")
            raise
        except Exception as e:
            logger.error(f"[{self.conn_id.hex()}] Send loop error: {e}")
        finally:
            await self.close()

    async def send_data(self, data: bytes):
        """发送数据到目标服务器"""
        # 如果连接还没建立，缓存数据（乐观发送模式）
        if not self._connected:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"[{self.conn_id.hex()}] Buffering early data ({len(data)} bytes)")
            self._early_data_buffer.append(data)
            return

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
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"[{self.conn_id.hex()}] Already in closing process, skipping")
            return

        self._closing = True

        if not self.running:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"[{self.conn_id.hex()}] Already closed (running=False)")
            return

        self.running = False
        logger.info(f"[{self.conn_id.hex()}] Closing target connection")

        try:
            # 立即取消 read_loop 和 send_loop 任务
            if self._read_task:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(f"[{self.conn_id.hex()}] Read task status: done={self._read_task.done()}, cancelled={self._read_task.cancelled()}")
                if not self._read_task.done():
                    logger.info(f"[{self.conn_id.hex()}] Cancelling read task")
                    self._read_task.cancel()
                    try:
                        await self._read_task
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug(f"[{self.conn_id.hex()}] Read task await completed")
                    except asyncio.CancelledError:
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug(f"[{self.conn_id.hex()}] Read task cancelled")
                    except Exception as e:
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug(f"[{self.conn_id.hex()}] Read task error: {e}")
            else:
                logger.warning(f"[{self.conn_id.hex()}] No read task found!")

            # 取消 send_loop 任务
            if self._send_task:
                if not self._send_task.done():
                    logger.info(f"[{self.conn_id.hex()}] Cancelling send task")
                    self._send_task.cancel()
                    try:
                        await self._send_task
                    except asyncio.CancelledError:
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug(f"[{self.conn_id.hex()}] Send task cancelled")
                    except Exception as e:
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug(f"[{self.conn_id.hex()}] Send task error: {e}")

            # 关闭写入端
            if self.writer:
                logger.info(f"[{self.conn_id.hex()}] Closing writer")
                try:
                    self.writer.close()
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(f"[{self.conn_id.hex()}] Writer closed, waiting...")
                    # wait_closed() 是 Python 3.7+ 才有的方法
                    if hasattr(self.writer, 'wait_closed'):
                        await self.writer.wait_closed()
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug(f"[{self.conn_id.hex()}] Writer wait_closed completed")
                except Exception as e:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(f"[{self.conn_id.hex()}] Writer close error: {e}")
            else:
                logger.warning(f"[{self.conn_id.hex()}] No writer to close")

        finally:
            # 无论如何都要通知客户端关闭（即使前面出错）
            # close_connection 使用锁确保所有 send_data 完成后再关闭
            logger.info(f"[{self.conn_id.hex()}] Notifying client to close")
            try:
                await self.ws_handler.close_connection(self.conn_id)
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(f"[{self.conn_id.hex()}] Client close notification sent")
            except Exception as e:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(f"[{self.conn_id.hex()}] Failed to notify client close: {e}")
