"""事件循环优化模块

提供可选的 uvloop 支持，如果 uvloop 不可用则回退到标准 asyncio。
uvloop 是一个基于 libuv 的高性能事件循环实现，可以提升 20-30% 的性能。
"""

import asyncio
import logging
import sys

logger = logging.getLogger(__name__)


def setup_event_loop():
    """设置事件循环（如果可用则使用 uvloop）

    Returns:
        bool: 如果成功启用 uvloop 返回 True，否则返回 False
    """
    # Windows 不支持 uvloop
    if sys.platform == 'win32':
        logger.info("⚠️  Running on Windows, uvloop not available (using default asyncio)")
        return False

    try:
        import uvloop

        # 尝试设置 uvloop 作为默认事件循环策略
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

        logger.info("✅ Using uvloop (high performance event loop, ~20-30% faster)")
        return True

    except ImportError:
        logger.info("⚠️  uvloop not installed, using default asyncio")
        logger.info("   To enable uvloop: pip install uvloop")
        return False

    except Exception as e:
        logger.warning(f"⚠️  Failed to initialize uvloop: {e}")
        logger.warning("   Falling back to default asyncio")
        return False


def get_event_loop_info():
    """获取当前事件循环的信息

    Returns:
        dict: 包含事件循环类型和相关信息的字典
    """
    loop = asyncio.get_event_loop()
    loop_class = loop.__class__.__name__

    info = {
        'loop_class': loop_class,
        'is_uvloop': 'uvloop' in loop_class.lower(),
        'platform': sys.platform,
    }

    return info
