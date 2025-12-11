import asyncio
import json
import argparse
from wsocks.client.socks5_server import SOCKS5Server
from wsocks.client.ws_client import WebSocketClient
from wsocks.common.logger import setup_logger

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

    logger.info("Starting SOCKS5-WS Proxy Client")

    # 创建 SOCKS5 服务器（先创建以便 ws_client 引用）
    socks5_server = None

    # 创建 WebSocket 客户端
    ws_client = WebSocketClient(
        config['server']['url'],
        config['server']['password'],
        None,  # 稍后设置
        ping_interval=config['server'].get('ping_interval', 30),
        ping_timeout=config['server'].get('ping_timeout', 10),
        compression=config['server'].get('compression', True),
        pool_size=config['server'].get('ws_pool_size', 8),
        heartbeat_enabled=config['server'].get('heartbeat_enabled', True),
        heartbeat_min=config['server'].get('heartbeat_min', 20),
        heartbeat_max=config['server'].get('heartbeat_max', 50)
    )

    # 创建 SOCKS5 服务器
    socks5_server = SOCKS5Server(
        config['local']['host'],
        config['local']['port'],
        ws_client
    )
    ws_client.socks5_server = socks5_server

    # 启动两个任务
    await asyncio.gather(
        socks5_server.start(),
        ws_client.connect()
    )

def main():
    """Entry point for console script"""
    asyncio.run(async_main())

if __name__ == '__main__':
    main()
