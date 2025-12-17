import asyncio
import json
import argparse
from wsocks.client.socks5_server import SOCKS5Server
from wsocks.client.ws_client import WebSocketClient
from wsocks.common.logger import setup_logger
from wsocks.common.event_loop import setup_event_loop

logger = setup_logger()

async def async_main():
    # 解析命令行参数
    parser = argparse.ArgumentParser(description='SOCKS5-WS Proxy Client')
    parser.add_argument('-c', '--config',
                       default='config_client.json',
                       help='配置文件路径 (默认: config_client.json)')
    args = parser.parse_args()

    # 加载配置
    logger.info(f"Loading config from: {args.config}")
    try:
        with open(args.config, 'r') as f:
            config = json.load(f)
    except FileNotFoundError:
        logger.error(f"配置文件不存在: {args.config}")
        return
    except json.JSONDecodeError as e:
        logger.error(f"配置文件格式错误: {e}")
        return

    # 应用配置文件中的日志等级
    log_level = config.get('log_level', 'INFO')
    setup_logger(log_level)
    logger.info(f"Log level set to: {log_level}")

    logger.info("Starting SOCKS5-WS Proxy Client")

    # 创建 SOCKS5 服务器（先创建以便 ws_client 引用）
    socks5_server = None
    ws_client = None

    try:
        # 创建 WebSocket 客户端
        ws_client = WebSocketClient(
            config['server']['url'],
            config['server']['password'],
            None,  # 稍后设置
            ping_interval=config['server'].get('ping_interval', 30),
            ping_timeout=config['server'].get('ping_timeout', 10),
            compression=config['server'].get('compression', False),
            pool_size=config['server'].get('ws_pool_size', 8),
            heartbeat_enabled=config['server'].get('heartbeat_enabled', True),
            heartbeat_min=config['server'].get('heartbeat_min', 20),
            heartbeat_max=config['server'].get('heartbeat_max', 50),
            use_fingerprint=config['server'].get('use_fingerprint', False),
            impersonate=config['server'].get('impersonate', 'chrome124')
        )

        # 创建 SOCKS5 服务器
        udp_config = config.get('udp', {})
        socks5_server = SOCKS5Server(
            config['local']['host'],
            config['local']['port'],
            ws_client,
            udp_enabled=udp_config.get('enabled', False),
            udp_timeout=udp_config.get('timeout', 60)
        )
        ws_client.socks5_server = socks5_server

        # 启动两个任务
        await asyncio.gather(
            socks5_server.start(),
            ws_client.connect()
        )
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        # 清理资源
        if ws_client:
            try:
                await ws_client.close()
            except Exception as cleanup_error:
                logger.debug(f"Error during cleanup: {cleanup_error}")
        raise

def main():
    """Entry point for console script"""
    # 设置高性能事件循环（如果可用）
    setup_event_loop()

    # 创建新的事件循环
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(async_main())
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:
        # 错误已经在 async_main 中记录过了，这里只需要退出
        pass
    finally:
        # 取消所有待处理的任务
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        # 等待所有任务取消完成
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()

if __name__ == '__main__':
    main()
