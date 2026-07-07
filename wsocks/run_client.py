import asyncio
import json
import os
import re
import argparse
import time
from wsocks.client.socks5_server import SOCKS5Server
from wsocks.client.ws_client import WebSocketClient
from wsocks.client.router import Router
from wsocks.common.logger import setup_logger
from wsocks.common.event_loop import setup_event_loop

logger = setup_logger()

# --direct-lan 使用的内置路由：私有网段直连，其余走代理
LAN_DIRECT_ROUTING = {
    "default": "proxy",
    "rules": [
        {
            "type": "ip_cidr",
            "value": ["127.0.0.0/8", "192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12"],
            "action": "direct",
        }
    ],
}


def build_parser():
    parser = argparse.ArgumentParser(description='SOCKS5-WS Proxy Client')
    # 配置文件（可选）。不指定且默认文件存在时自动加载，以保持向后兼容
    parser.add_argument('-c', '--config',
                        default=None,
                        help='配置文件路径 (默认: config_client.json，存在则自动加载)')
    # 无配置文件启动 / 覆盖配置文件的参数
    parser.add_argument('-s', '--server', dest='server', default=None,
                        help='WebSocket 服务器地址 (ws:// 或 wss://)')
    parser.add_argument('-k', '--password', dest='password', default=None,
                        help='连接密码')
    parser.add_argument('-l', '--listen', dest='listen', default=None,
                        help='本地 SOCKS5 监听地址 HOST:PORT (默认: 127.0.0.1:28888)')
    parser.add_argument('-m', '--crypto', dest='crypto', default=None,
                        help='加密方式 (如 chacha20)')
    parser.add_argument('--pool-size', dest='pool_size', type=int, default=None,
                        help='WebSocket 连接池大小 (默认: 8)')
    parser.add_argument('--compression', dest='compression', action='store_true', default=None,
                        help='启用压缩')
    parser.add_argument('--no-compression', dest='compression', action='store_false',
                        help='禁用压缩')
    parser.add_argument('--proxy', dest='proxy', default=None,
                        help='上游代理地址')
    parser.add_argument('--fingerprint', dest='fingerprint', action='store_true', default=None,
                        help='启用 TLS 指纹伪装 (需 Python>=3.10 与 curl_cffi)')
    parser.add_argument('--impersonate', dest='impersonate', default=None, metavar='BROWSER',
                        help='伪装的浏览器指纹，指定后自动启用指纹伪装。'
                             '可选: chrome99~chrome136 / safari153~safari260 / firefox133 / firefox135'
                             ' (默认: chrome124)')
    parser.add_argument('--udp', dest='udp', action='store_true', default=None,
                        help='启用 UDP 转发')
    parser.add_argument('--direct-lan', dest='direct_lan', action='store_true', default=False,
                        help='私有网段直连、其余走代理的内置路由')
    parser.add_argument('--log-level', dest='log_level', default=None,
                        help='日志等级 (默认: INFO)')
    return parser


def load_config_file(path):
    with open(path, 'r') as f:
        text = f.read()
    # 去除 // 和 /* */ 注释，但不影响字符串内容
    text = re.sub(r'("(?:[^"\\]|\\.)*")|//.*?$|/\*.*?\*/', lambda m: m.group(1) or '', text, flags=re.DOTALL | re.MULTILINE)
    return json.loads(text)


def apply_overrides(config, args):
    """将命令行参数覆盖到 config 上（仅覆盖显式提供的参数）"""
    server = config.setdefault('server', {})
    if args.server is not None:
        server['url'] = args.server
    if args.password is not None:
        server['password'] = args.password
    if args.crypto is not None:
        server['crypto_method'] = args.crypto
    if args.pool_size is not None:
        server['ws_pool_size'] = args.pool_size
    if args.compression is not None:
        server['compression'] = args.compression
    if args.proxy is not None:
        server['proxy'] = args.proxy
    if args.fingerprint is not None:
        server['use_fingerprint'] = args.fingerprint
    if args.impersonate is not None:
        server['impersonate'] = args.impersonate
        # 指定浏览器指纹时自动启用指纹伪装（除非用户已显式设置 --fingerprint）
        if args.fingerprint is None:
            server['use_fingerprint'] = True

    if args.listen is not None:
        host, sep, port = args.listen.rpartition(':')
        if not sep:
            raise ValueError(f"--listen 格式应为 HOST:PORT，收到: {args.listen}")
        local = config.setdefault('local', {})
        local['host'] = host
        local['port'] = int(port)

    if args.udp is not None:
        config.setdefault('udp', {})['enabled'] = args.udp

    if args.direct_lan:
        config['routing'] = LAN_DIRECT_ROUTING

    if args.log_level is not None:
        config['log_level'] = args.log_level

    return config


async def async_main():
    # 解析命令行参数
    parser = build_parser()
    args = parser.parse_args()

    # 确定配置来源：显式 -c > 默认 config_client.json（存在时）> 纯命令行参数
    config = {}
    config_path = args.config
    if config_path is None and os.path.exists('config_client.json'):
        config_path = 'config_client.json'

    if config_path is not None:
        logger.info(f"Loading config from: {config_path}")
        try:
            config = load_config_file(config_path)
        except FileNotFoundError:
            logger.error(f"配置文件不存在: {config_path}")
            return
        except json.JSONDecodeError as e:
            logger.error(f"配置文件格式错误: {e}")
            return
    else:
        logger.info("未指定配置文件，使用命令行参数启动")

    # 命令行参数覆盖配置文件
    try:
        config = apply_overrides(config, args)
    except ValueError as e:
        logger.error(str(e))
        return

    # 校验必填项
    server_cfg = config.get('server', {})
    if not server_cfg.get('url') or not server_cfg.get('password'):
        parser.error("缺少必要参数：请通过 -c 提供配置文件，或同时指定 -s 服务器地址和 -k 密码")

    # 应用日志等级
    log_level = config.get('log_level', 'INFO')
    setup_logger(log_level)
    logger.info(f"Log level set to: {log_level}")

    logger.info("Starting SOCKS5-WS Proxy Client")

    # 创建 SOCKS5 服务器（先创建以便 ws_client 引用）
    socks5_server = None
    ws_client = None

    try:
        # 创建 WebSocket 客户端
        crypto_method = config['server'].get('crypto_method', None)
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
            impersonate=config['server'].get('impersonate', 'chrome124'),
            crypto_method=crypto_method,
            proxy=config['server'].get('proxy'),
            impersonate_class_version=config['server'].get('impersonate_class_version', 2)
        )
        if not crypto_method and config['server']['url'].startswith('ws://'):
            for _ in range(3):
                time.sleep(.5)
                logger.warning('crypto_method is not set, but the url is ws://, this is not recommended !!!')

        # 创建路由器（可选）
        routing_config = config.get('routing')
        router = Router(routing_config) if routing_config else None

        # 创建 SOCKS5 服务器
        udp_config = config.get('udp', {})
        local_config = config.get('local', {})
        socks5_server = SOCKS5Server(
            local_config.get('host', '127.0.0.1'),
            local_config.get('port', 28888),
            ws_client,
            udp_enabled=udp_config.get('enabled', False),
            udp_timeout=udp_config.get('timeout', 60),
            router=router,
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
