import json
import argparse
import tornado.ioloop
import tornado.web
from wsocks.server.ws_handler import WebSocketHandler
from wsocks.common.logger import setup_logger
from wsocks.common.event_loop import setup_event_loop

logger = setup_logger()

def main():
    # 解析命令行参数
    parser = argparse.ArgumentParser(description='SOCKS5-WS Proxy Server')
    parser.add_argument('-c', '--config',
                       default='config_server.json',
                       help='配置文件路径 (默认: config_server.json)')
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

    # 设置高性能事件循环（如果可用）
    # Tornado 会自动使用 asyncio 的事件循环策略
    setup_event_loop()

    logger.info("Starting SOCKS5-WS Proxy Server")

    # 创建应用
    app = tornado.web.Application([
        (config['server']['path'], WebSocketHandler, {
            'password': config['server']['password'],
            'timeout': config['server'].get('timeout', 30.0),
            'max_connections': config['server'].get('max_connections', 1000),
            'buffer_size': config['server'].get('buffer_size', 65536)
        })
    ])

    # 监听端口
    app.listen(config['server']['port'], address=config['server']['host'])

    logger.info(f"Server listening on {config['server']['host']}:{config['server']['port']}")

    # 启动事件循环
    tornado.ioloop.IOLoop.current().start()

if __name__ == '__main__':
    main()
