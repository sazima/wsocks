import logging
import sys

def setup_logger(level: str = "INFO"):
    """设置日志"""
    logger = logging.getLogger("socks5-ws-proxy")
    logger.setLevel(getattr(logging, level.upper()))

    # 避免重复添加 handler
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - '
            '%(pathname)s:%(lineno)d - %(message)s'
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger
