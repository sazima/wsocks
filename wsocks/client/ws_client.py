import asyncio
import logging

import random
import time
import os
import traceback
from typing import List, Optional
from wsocks.common.protocol import Protocol, MSG_TYPE_DATA, MSG_TYPE_CLOSE, MSG_TYPE_CONNECT_SUCCESS, MSG_TYPE_CONNECT_FAILED, MSG_TYPE_HEARTBEAT, MSG_TYPE_UDP_DATA
from wsocks.common.logger import setup_logger
from wsocks.client.ws_adapter import create_ws_adapter, WebSocketAdapter

logger = setup_logger()

class WebSocketClient:
    """WebSocket 客户端（支持连接池）"""
    def __init__(self, url: str, password: str, socks5_server, ping_interval: float = 30, ping_timeout: float = 10, compression: bool = True, pool_size: int = 8,
                 heartbeat_enabled: bool = True, heartbeat_min: float = 20, heartbeat_max: float = 50,
                 use_fingerprint: bool = False, impersonate: str = "chrome124"):
        self.url = url
        self.password = password
        self.socks5_server = socks5_server
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.compression = 'deflate' if compression else None
        self.pool_size = pool_size
        self.ws_pool: List[Optional[WebSocketAdapter]] = [None] * pool_size
        self.running = False
        self.next_ws_index = 0  # 轮询索引

        # TLS 指纹伪装配置
        self.use_fingerprint = use_fingerprint
        self.impersonate = impersonate

        # 应用层心跳配置
        self.heartbeat_enabled = heartbeat_enabled
        self.heartbeat_min = heartbeat_min
        self.heartbeat_max = heartbeat_max
        self.last_activity_time: List[float] = [0] * pool_size  # 每个连接的最后活动时间
        self.last_business_activity_time: List[float] = [0] * pool_size
        self.last_heartbeat_time: List[float] = [0] * pool_size

        # 动态连接池管理
        self.target_ws_count = 1  # 当前目标连接数，初始为1
        self.ws_tasks: List[Optional[asyncio.Task]] = [None] * pool_size  # 每个连接的任务
        self.scale_down_task: Optional[asyncio.Task] = None  # 缩容延迟任务
        self.scale_down_target: Optional[int] = None  # 缩容任务的目标值
        self.monitor_task: Optional[asyncio.Task] = None  # 监控任务

    async def connect(self):
        """连接到服务器（初始创建多个连接以减少冷启动延迟，后续动态扩展）"""
        self.running = True

        # 初始创建 2 个连接（或 pool_size 的一半，取较小值）以减少冷启动延迟
        initial_count = min(2, max(1, self.pool_size // 2))
        logger.info(f"Creating {initial_count} initial WebSocket connections (pool_size={self.pool_size}, dynamic scaling enabled)")
        self.target_ws_count = initial_count

        # 创建初始连接
        for i in range(initial_count):
            task = asyncio.ensure_future(self._connect_single(i))
            self.ws_tasks[i] = task

        # 启动监控任务（定期检查并调整连接数）
        self.monitor_task = asyncio.ensure_future(self._monitor_and_scale())

        # 等待初始连接启动
        await asyncio.sleep(0.2)  # 稍微增加等待时间，确保连接建立

    async def _connect_single(self, index: int):
        """连接单个 WebSocket（带重连）"""
        try:
            while self.running:
                try:
                    fingerprint_info = f", fingerprint={self.impersonate}" if self.use_fingerprint else ""
                    logger.info(f"[WS-{index}] Connecting to {self.url}{fingerprint_info}")

                    # 创建适配器
                    adapter = create_ws_adapter(
                        use_fingerprint=self.use_fingerprint,
                        impersonate=self.impersonate
                    )

                    # 连接参数
                    connect_kwargs = {}
                    if not self.use_fingerprint:
                        # 标准 websockets 支持这些参数
                        connect_kwargs['ping_interval'] = None if self.heartbeat_enabled else self.ping_interval
                        connect_kwargs['ping_timeout'] = None if self.heartbeat_enabled else self.ping_timeout
                        connect_kwargs['compression'] = self.compression

                    # 连接
                    ws = await adapter.connect(self.url, **connect_kwargs)

                    self.ws_pool[index] = ws
                    self.last_activity_time[index] = time.time()
                    self.last_business_activity_time[index] = 0
                    self.last_heartbeat_time[index] = 0

                    heartbeat_mode = "app-layer random" if self.heartbeat_enabled else f"native {self.ping_interval}s"
                    adapter_type = "CurlCffi" if self.use_fingerprint else "WebSockets"
                    logger.info(f"[WS-{index}] Connected (adapter={adapter_type}, compression={self.compression}, heartbeat={heartbeat_mode})")

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

                    logger.debug(f"[WS-{index}] Connection error: {traceback.format_exc()}")
                    self.ws_pool[index] = None
                    await asyncio.sleep(5)  # 重连延迟

        except asyncio.CancelledError:
            # 任务被取消（缩容时），清理并退出
            logger.info(f"[WS-{index}] Task cancelled (scale down)")
            self.ws_pool[index] = None
            raise

    async def _receive_loop(self, index: int, ws: WebSocketAdapter):
        """接收消息循环（单个连接）"""
        try:
            async for message in ws:
                self.last_activity_time[index] = time.time()  # 更新活动时间
                await self.handle_message(message)
        except Exception as e:
            # 兼容不同适配器的连接关闭异常
            if "ConnectionClosed" in str(type(e).__name__) or "closed" in str(e).lower():
                logger.warning(f"[WS-{index}] Connection closed")
            else:
                logger.error(f"[WS-{index}] Receive error: {traceback.format_exc()}")
            # 先关闭连接再清空
            if self.ws_pool[index]:
                try:
                    await self.ws_pool[index].close()
                except:
                    pass
            self.ws_pool[index] = None

    async def _heartbeat_loop(self, index: int, ws: WebSocketAdapter):
        try:
            while self.running and self.ws_pool[index] is not None:
                active_socks = self._get_active_socks_count()
                is_idle = (active_socks == 0)

                # 心跳间隔
                if is_idle:
                    interval = self.heartbeat_max * random.uniform(0.8, 1.0)
                else:
                    interval = random.uniform(self.heartbeat_min, self.heartbeat_max)

                await asyncio.sleep(interval)

                if self.ws_pool[index] is None:
                    break

                now = time.time()

                # ★ 只看「业务活动」
                last_business = self.last_business_activity_time[index]
                time_since_business = (
                    now - last_business if last_business > 0 else float('inf')
                )

                # 最近有业务流量 → 跳过心跳
                threshold = self.heartbeat_max if is_idle else self.heartbeat_min
                if time_since_business < threshold:
                    logger.debug(
                        f"[WS-{index}] Skip heartbeat "
                        f"(recent business activity: {time_since_business:.1f}s ago)"
                    )
                    continue

                # 构造心跳数据
                data_size = random.randint(100, 2000)
                heartbeat_data = os.urandom(data_size)
                heartbeat_conn_id = b'\x00\x00\x00\x00'

                packed_data = Protocol.pack(
                    MSG_TYPE_HEARTBEAT,
                    heartbeat_conn_id,
                    heartbeat_data,
                    self.password
                )

                try:
                    # 心跳使用高优先级
                    await ws.send(packed_data, priority=True, conn_id=heartbeat_conn_id)
                    self.last_heartbeat_time[index] = now
                    self.last_activity_time[index] = now

                    mode = "idle" if is_idle else "active"
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(
                            f"[WS-{index}] Sent heartbeat "
                            f"({data_size} bytes, mode={mode})"
                        )

                except Exception as e:
                    logger.warning(f"[WS-{index}] Heartbeat send failed: {e}")
                    break

        except asyncio.CancelledError:
            logger.debug(f"[WS-{index}] Heartbeat loop cancelled")

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
                    self.last_business_activity_time[
                        int.from_bytes(conn_id, 'big') % self.pool_size
                        ] = time.time()
                    await connection.send_to_client(data)
                else:
                    if logger.isEnabledFor(logging.DEBUG):
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

            elif msg_type == MSG_TYPE_UDP_DATA:
                # UDP 数据消息，转发到 UDP relay
                udp_relay = self.socks5_server.get_udp_relay()
                if udp_relay:
                    # 解析 UDP 数据包
                    import msgpack
                    udp_packet = msgpack.unpackb(data, raw=False)
                    dst_addr = udp_packet['dst_addr']
                    dst_port = udp_packet['dst_port']
                    payload = bytes.fromhex(udp_packet['data'])
                    self.last_business_activity_time[
                        int.from_bytes(conn_id, 'big') % self.pool_size
                        ] = time.time()
                    await udp_relay.send_to_client(conn_id, dst_addr, dst_port, payload)
                else:
                    logger.warning(f"[{conn_id.hex()}] UDP relay not available")

        except Exception as e:
            logger.error(f"Handle message error: {e}")

    async def send_message(self, msg_type: int, conn_id: bytes, data: bytes):
        """发送消息（负载均衡到连接池）"""
        if not self.running:
            raise Exception("Not connected")
        # 确保 data 是纯 bytes
        if not isinstance(data, bytes):
            data = bytes(data)

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
                    self.last_activity_time[current_index] = time.time()
                    self.last_business_activity_time[current_index] = time.time()
                    # 发送消息时携带 conn_id，用于关闭连接时过滤队列
                    priority = (msg_type == MSG_TYPE_HEARTBEAT)  # 心跳高优先级
                    await ws.send(packed_data, priority=priority, conn_id=conn_id)
                    self.last_activity_time[current_index] = time.time()  # 更新活动时间
                    return
                except Exception as e:
                    logger.warning(f"[WS-{current_index}] Send failed: {e}, trying next...")
                    print(traceback.format_exc())
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
                self.scale_down_target = None

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
            # 已有相同目标缩容任务 → 不重复调度
            if (
                    self.scale_down_task
                    and not self.scale_down_task.done()
                    and self.scale_down_target == target
            ):
                logger.debug(f"[Pool] Scale down task already scheduled for target={target}, skipping")
                return

            # 调试：记录为什么需要重新调度
            if self.scale_down_task:
                logger.debug(f"[Pool] Re-scheduling scale down: task_done={self.scale_down_task.done()}, old_target={self.scale_down_target}, new_target={target}")

            # 计算 delay（只算一次）
            if active_socks == 0:
                delay = random.uniform(180, 300)
                logger.info(
                    f"[Pool] Scheduling scale down to idle mode: "
                    f"{current_count} → {target} WS (delay={delay:.0f}s)"
                )
            else:
                delay = random.uniform(60, 120)
                logger.info(
                    f"[Pool] Scheduling scale down: "
                    f"{current_count} → {target} WS "
                    f"(delay={delay:.0f}s, active_socks={active_socks})"
                )

            # 取消旧任务（仅当目标不同且还在运行时）
            if self.scale_down_task and not self.scale_down_task.done() and self.scale_down_target != target:
                logger.debug(f"[Pool] Cancelling old scale down task (old_target={self.scale_down_target}, new_target={target})")
                self.scale_down_task.cancel()

            # 创建新任务
            self.scale_down_target = target
            self.scale_down_task = asyncio.ensure_future(
                self._scale_down_delayed(target, delay)
            )


    async def _scale_down_delayed(self, target: int, delay: float):
        """延迟缩容（防止抖动）"""
        try:
            await asyncio.sleep(delay)

            # 再次检查是否仍需缩容
            current_count = sum(1 for ws in self.ws_pool if ws is not None)
            current_target = self._calculate_target_ws()

            # 如果当前连接数已经 <= 目标，或者重新计算的目标 > 缩容目标，取消缩容
            if current_count <= target or current_target > target:
                logger.info(f"[Pool] Scale down cancelled: current={current_count}, target={target}, recalc_target={current_target}")
                # 只有当 target 仍然匹配时才清理（避免覆盖新任务的设置）
                if self.scale_down_target == target:
                    self.scale_down_target = None
                return

            # 执行缩容
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
            # 缩容完成，清除目标（只有当 target 仍然匹配时才清理）
            if self.scale_down_target == target:
                self.scale_down_target = None

        except asyncio.CancelledError:
            logger.debug("[Pool] Scale down task cancelled")
            # 只有当 target 仍然匹配时才清理（避免覆盖新任务的设置）
            if self.scale_down_target == target:
                self.scale_down_target = None
        except Exception as e:
            logger.error(f"[Pool] Scale down error: {e}")
            # 只有当 target 仍然匹配时才清理（避免覆盖新任务的设置）
            if self.scale_down_target == target:
                self.scale_down_target = None

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
