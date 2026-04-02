"""日志系统模块"""

from loguru import logger
import sys
from pathlib import Path


def setup_logger(log_file: str = "logs/quantbot.log", level: str = "INFO"):
    """
    配置日志系统
    
    Args:
        log_file: 日志文件路径
        level: 日志级别 (DEBUG, INFO, WARNING, ERROR)
    """
    # 移除默认处理器
    logger.remove()
    
    # 控制台输出（彩色）
    logger.add(
        sys.stdout,
        level=level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        colorize=True
    )
    
    # 确保日志目录存在
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    
    # 文件输出
    logger.add(
        log_file,
        level=level,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
        rotation="10 MB",      # 日志文件大小达到 10MB 时轮转
        retention="7 days",    # 保留 7 天的日志
        compression="zip",     # 压缩旧日志
        encoding="utf-8"
    )
    
    return logger


# 导出 logger 实例
__all__ = ["logger", "setup_logger"]