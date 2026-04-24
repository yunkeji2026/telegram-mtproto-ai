"""
日志设置模块
配置和管理应用程序日志
"""

import logging
import sys
from pathlib import Path
from typing import Optional
import colorama
from colorama import Fore, Style

# 初始化colorama
colorama.init()


class ColoredFormatter(logging.Formatter):
    """带颜色的日志格式化器"""
    
    COLORS = {
        'DEBUG': Fore.CYAN,
        'INFO': Fore.GREEN,
        'WARNING': Fore.YELLOW,
        'ERROR': Fore.RED,
        'CRITICAL': Fore.RED + Style.BRIGHT
    }
    
    def format(self, record):
        """格式化日志记录"""
        if record.levelname in self.COLORS:
            color = self.COLORS[record.levelname]
            record.levelname = f"{color}{record.levelname}{Style.RESET_ALL}"
            record.msg = f"{color}{record.msg}{Style.RESET_ALL}"
        
        return super().format(record)


def setup_logger(
    name: str = "ai_chat_assistant",
    log_level: str = "INFO",
    log_file: Optional[str] = None,
    console_output: bool = True
) -> logging.Logger:
    """
    设置日志记录器
    
    Args:
        name: 日志记录器名称
        log_level: 日志级别 (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: 日志文件路径，如果为None则不保存到文件
        console_output: 是否输出到控制台
        
    Returns:
        配置好的日志记录器
    """
    # 创建日志记录器
    logger = logging.getLogger(name)
    
    # 设置日志级别
    level = getattr(logging, log_level.upper(), logging.INFO)
    logger.setLevel(level)
    
    # 清除现有的处理器
    logger.handlers.clear()
    
    # 控制台处理器
    if console_output:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        
        # 控制台格式化器（带颜色）
        console_formatter = ColoredFormatter(
            '[%(asctime)s] [%(levelname)s] %(name)s: %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        console_handler.setFormatter(console_formatter)
        logger.addHandler(console_handler)
    
    # 文件处理器
    if log_file:
        # 确保日志目录存在
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setLevel(level)
        
        # 文件格式化器（不带颜色）
        file_formatter = logging.Formatter(
            '[%(asctime)s] [%(levelname)s] %(name)s: %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)
    
    return logger


def get_logger(name: str = "ai_chat_assistant") -> logging.Logger:
    """
    获取日志记录器
    
    Args:
        name: 日志记录器名称
        
    Returns:
        日志记录器实例
    """
    return logging.getLogger(name)


class LoggerMixin:
    """日志混入类，方便其他类使用日志"""
    
    @property
    def logger(self) -> logging.Logger:
        """获取日志记录器"""
        if not hasattr(self, '_logger'):
            class_name = self.__class__.__name__
            self._logger = get_logger(f"ai_chat_assistant.{class_name}")
        return self._logger


def log_function_call(func):
    """函数调用日志装饰器"""
    def wrapper(*args, **kwargs):
        logger = get_logger("function_calls")
        
        # 获取函数名和参数
        func_name = func.__name__
        args_str = ", ".join([str(arg) for arg in args[1:]]) if len(args) > 1 else ""
        kwargs_str = ", ".join([f"{k}={v}" for k, v in kwargs.items()])
        params = ", ".join(filter(None, [args_str, kwargs_str]))
        
        logger.debug(f"调用 {func_name}({params})")
        
        try:
            result = func(*args, **kwargs)
            logger.debug(f"{func_name} 调用成功")
            return result
        except Exception as e:
            logger.error(f"{func_name} 调用失败: {e}")
            raise
    
    return wrapper


def log_async_function_call(func):
    """异步函数调用日志装饰器"""
    async def wrapper(*args, **kwargs):
        logger = get_logger("function_calls")
        
        # 获取函数名和参数
        func_name = func.__name__
        args_str = ", ".join([str(arg) for arg in args[1:]]) if len(args) > 1 else ""
        kwargs_str = ", ".join([f"{k}={v}" for k, v in kwargs.items()])
        params = ", ".join(filter(None, [args_str, kwargs_str]))
        
        logger.debug(f"异步调用 {func_name}({params})")
        
        try:
            result = await func(*args, **kwargs)
            logger.debug(f"{func_name} 异步调用成功")
            return result
        except Exception as e:
            logger.error(f"{func_name} 异步调用失败: {e}")
            raise
    
    return wrapper


# 模块初始化时创建默认日志记录器
_default_logger = setup_logger()


def get_default_logger() -> logging.Logger:
    """获取默认日志记录器"""
    return _default_logger