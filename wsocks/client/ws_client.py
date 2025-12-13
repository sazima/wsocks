import asyncio
import websockets
import random
import time
import os
from typing import List, Optional
from wsocks.common.protocol import Protocol, MSG_TYPE_DATA, MSG_TYPE_CLOSE, MSG_TYPE_CONNECT_SUCCESS, MSG_TYPE_CONNECT_FAILED, MSG_TYPE_HEARTBEAT
from wsocks.common.logger import setup_logger

logger = setup_logger()

class WebSocketClient:
    """WebSocket 客户端（支持连接池）"""
    def __init__(self, url: str, password: str, socks5_server, ping_interval: float = 30, ping_timeout: float = 10, compression: bool = True, pool_size: int = 8,
                 heartbeat_enabled: bool = True, heartbeat_min: float = 20, heartbeat_max: float = 50):
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

        # 应用层心跳配置
        self.heartbeat_enabled = heartbeat_enabled
        self.heartbeat_min = heartbeat_min
        self.heartbeat_max = heartbeat_max
        self.last_activity_time: List[float] = [0] * pool_size  # 每个连接的最后活动时间

        # 动态连接池管理
        self.target_ws_count = 1  # 当前目标连接数，初始为1
        self.ws_tasks: List[Optional[asyncio.Task]] = [None] * pool_size  # 每个连接的任务
        self.scale_down_task: Optional[asyncio.Task] = None  # 缩容延迟任务
        self.monitor_task: Optional[asyncio.Task] = None  # 监控任务

    async def connect(self):
        """连接到服务器（初始创建1个连接，后续动态扩展）"""
        self.running = True

        # 初始只创建1个连接（闲置模式）
        logger.info(f"Creating initial WebSocket connection (pool_size={self.pool_size}, dynamic scaling enabled)")
        self.target_ws_count = 1
        task = asyncio.ensure_future(self._connect_single(0))
        self.ws_tasks[0] = task

        # 启动监控任务（定期检查并调整连接数）
        self.monitor_task = asyncio.ensure_future(self._monitor_and_scale())

        # 等待初始连接启动
        await asyncio.sleep(0.1)

    async def _connect_single(self, index: int):
        """连接单个 WebSocket（带重连）"""
        try:
            while self.running:
                try:
                    logger.info(f"[WS-{index}] Connecting to {self.url}")

                    # 禁用原生 ping/pong 以避免固定时序特征
                    # 使用应用层随机心跳代替
                    ws = await websockets.connect(
                        self.url,
                        ping_interval=None if self.heartbeat_enabled else self.ping_interval,
                        ping_timeout=None if self.heartbeat_enabled else self.ping_timeout,
                        compression=self.compression
                    )
                    self.ws_pool[index] = ws
                    self.last_activity_time[index] = time.time()

                    heartbeat_mode = "app-layer random" if self.heartbeat_enabled else f"native {self.ping_interval}s"
                    logger.info(f"[WS-{index}] Connected (compression={self.compression}, heartbeat={heartbeat_mode})")

                    # 启动接收和心跳任务
                    receive_task = asyncio.ensure_future(self._receive_loop(index, ws))
                    heartbeat_task = asyncio.ensure_future(self._heartbeat_loop(index, ws)) if self.heartbeat_enabled else None

                    # 等待任意任务完成（连接断开）
                    tasks = [receive_task]
                    if heartbeat_task:
                        tasks.append(heartbeat_task)

                    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

                    # 取消未完成的任务
                    for task in pending:
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass

                except Exception as e:
                    logger.error(f"[WS-{index}] Connection error: {e}")
                    self.ws_pool[index] = None
                    await asyncio.sleep(5)  # 重连延迟

        except asyncio.CancelledError:
            # 任务被取消（缩容时），清理并退出
            logger.info(f"[WS-{index}] Task cancelled (scale down)")
            self.ws_pool[index] = None
            raise

    async def _receive_loop(self, index: int, ws: websockets.WebSocketClientProtocol):
        """接收消息循环（单个连接）"""
        try:
            async for message in ws:
                self.last_activity_time[index] = time.time()  # 更新活动时间
                await self.handle_message(message)
        except websockets.exceptions.ConnectionClosed:
            logger.warning(f"[WS-{index}] Connection closed")
            self.ws_pool[index] = None
        except Exception as e:
            logger.error(f"[WS-{index}] Receive error: {e}")
            self.ws_pool[index] = None

    async def _heartbeat_loop(self, index: int, ws: websockets.WebSocketClientProtocol):
        """应用层心跳循环（随机间隔和数据大小，支持闲置模式）"""
        try:
            while self.running and self.ws_pool[index] is not None:
                # 检查是否处于闲置模式
                active_socks = self._get_active_socks_count()
                is_idle = (active_socks == 0)

                # 根据模式选择心跳间隔
                if is_idle:
                    # 闲置模式：使用 heartbeat_max 周围的随机间隔（±20%）
                    interval = self.heartbeat_max * random.uniform(0.8, 1.2)
                else:
                    # 活跃模式：使用 heartbeat_min ~ heartbeat_max 随机间隔
                    interval = random.uniform(self.heartbeat_min, self.heartbeat_max)

                await asyncio.sleep(interval)

                # 检查连接是否仍然存活
                if self.ws_pool[index] is None:
                    break

                # 如果最近有业务流量，跳过心跳（避免不必要的特征）
                time_since_last_activity = time.time() - self.last_activity_time[index]
                min_interval = self.heartbeat_max if is_idle else self.heartbeat_min
                if time_since_last_activity < min_interval:
                    logger.debug(f"[WS-{index}] Skip heartbeat (recent activity: {time_since_last_activity:.1f}s ago)")
                    continue

                # 生成随机大小的心跳数据（100-2000 字节，模拟真实业务数据）
                data_size = random.randint(100, 2000)
                heartbeat_data = os.urandom(data_size)

                # 使用一个特殊的 conn_id 发送心跳
                heartbeat_conn_id = b'\x00\x00\x00\x00'
                packed_data = Protocol.pack(MSG_TYPE_HEARTBEAT, heartbeat_conn_id, heartbeat_data, self.password)

                try:
                    await ws.send(packed_data)
                    self.last_activity_time[index] = time.time()
                    mode = "idle" if is_idle else "active"
                    logger.debug(f"[WS-{index}] Sent heartbeat ({data_size} bytes, interval={interval:.1f}s, mode={mode})")
                except Exception as e:
                    logger.warning(f"[WS-{index}] Heartbeat send failed: {e}")
                    break

        except asyncio.CancelledError:
            logger.debug(f"[WS-{index}] Heartbeat loop cancelled")
        except Exception as e:
            logger.error(f"[WS-{index}] Heartbeat loop error: {e}")

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

            elif msg_type == MSG_TYPE_HEARTBEAT:
                # 心跳响应，忽略即可（仅用于保持连接）
                logger.debug(f"Received heartbeat response ({len(data)} bytes)")

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
                    self.last_activity_time[current_index] = time.time()  # 更新活动时间
                    return
                except Exception as e:
                    logger.warning(f"[WS-{current_index}] Send failed: {e}, trying next...")
                    self.ws_pool[current_index] = None
                    continue

        # 所有连接都不可用
        raise Exception("All WebSocket connections unavailable")

    def _get_active_socks_count(self) -> int:
        """获取当前活跃的 SOCKS5 连接数"""
        return len(self.socks5_server.connections)

    def _calculate_target_ws(self) -> int:
        """计算目标 WebSocket 连接数"""
        active_socks = self._get_active_socks_count()
        target = max(1, min(active_socks, self.pool_size))
        return target

    async def _scale_pool(self):
        """调整连接池大小（扩容/缩容）"""
        current_count = sum(1 for ws in self.ws_pool if ws is not None)
        target = self._calculate_target_ws()
        active_socks = self._get_active_socks_count()

        if target == current_count:
            return  # 无需调整

        if target > current_count:
            # 扩容：立即执行
            logger.info(f"[Pool] Scaling up: {current_count} → {target} WS connections (active_socks={active_socks})")

            # 取消之前的缩容任务（如果有）
            if self.scale_down_task and not self.scale_down_task.done():
                self.scale_down_task.cancel()
                self.scale_down_task = None

            # 创建新连接
            for i in range(self.pool_size):
                if self.ws_pool[i] is None and self.ws_tasks[i] is None:
                    if current_count >= target:
                        break
                    task = asyncio.ensure_future(self._connect_single(i))
                    self.ws_tasks[i] = task
                    current_count += 1

            self.target_ws_count = target

        elif target < current_count:
            # 缩容：延迟执行
            if active_socks == 0:
                delay = random.uniform(180, 300)  # 闲置模式：3-5分钟
                logger.info(f"[Pool] Scheduling scale down to idle mode: {current_count} → 1 WS (delay={delay:.0f}s)")
            else:
                delay = random.uniform(60, 120)  # 活跃模式：1-2分钟
                logger.info(f"[Pool] Scheduling scale down: {current_count} → {target} WS (delay={delay:.0f}s, active_socks={active_socks})")

            # 取消之前的缩容任务
            if self.scale_down_task and not self.scale_down_task.done():
                self.scale_down_task.cancel()

            # 启动新的缩容任务
            self.scale_down_task = asyncio.ensure_future(self._scale_down_delayed(target, delay))

    async def _scale_down_delayed(self, target: int, delay: float):
        """延迟缩容（防止抖动）"""
        try:
            await asyncio.sleep(delay)

            # 再次检查是否仍需缩容
            current_target = self._calculate_target_ws()
            if current_target >= target:
                logger.info(f"[Pool] Scale down cancelled: target changed to {current_target}")
                return

            # 执行缩容
            current_count = sum(1 for ws in self.ws_pool if ws is not None)
            active_socks = self._get_active_socks_count()
            logger.info(f"[Pool] Scaling down: {current_count} → {target} WS connections (active_socks={active_socks})")

            # 关闭多余的连接（从后往前关闭）
            closed = 0
            for i in range(self.pool_size - 1, -1, -1):
                if closed >= current_count - target:
                    break

                if self.ws_pool[i] is not None:
                    # 关闭连接
                    try:
                        await self.ws_pool[i].close()
                    except Exception as e:
                        logger.debug(f"[WS-{i}] Close error: {e}")

                    self.ws_pool[i] = None

                    # 取消任务
                    if self.ws_tasks[i] and not self.ws_tasks[i].done():
                        self.ws_tasks[i].cancel()
                        try:
                            await self.ws_tasks[i]
                        except asyncio.CancelledError:
                            pass
                    self.ws_tasks[i] = None

                    closed += 1

            self.target_ws_count = target

        except asyncio.CancelledError:
            logger.debug("[Pool] Scale down task cancelled")
        except Exception as e:
            logger.error(f"[Pool] Scale down error: {e}")

    async def _monitor_and_scale(self):
        """监控并定期调整连接池"""
        try:
            while self.running:
                await asyncio.sleep(10)  # 每10秒检查一次

                if not self.running:
                    break

                await self._scale_pool()

        except asyncio.CancelledError:
            logger.debug("[Pool] Monitor task cancelled")
        except Exception as e:
            logger.error(f"[Pool] Monitor error: {e}")

    async def close(self):
        """关闭所有连接并清理资源"""
        logger.info("[Pool] Shutting down WebSocket client...")
        self.running = False

        # 取消监控任务
        if self.monitor_task and not self.monitor_task.done():
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass

        # 取消缩容任务
        if self.scale_down_task and not self.scale_down_task.done():
            self.scale_down_task.cancel()
            try:
                await self.scale_down_task
            except asyncio.CancelledError:
                pass

        # 取消所有连接任务
        for i, task in enumerate(self.ws_tasks):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # 关闭所有 WebSocket 连接
        for i, ws in enumerate(self.ws_pool):
            if ws:
                try:
                    await ws.close()
                except Exception as e:
                    logger.debug(f"[WS-{i}] Close error: {e}")

        logger.info("[Pool] Shutdown complete")
