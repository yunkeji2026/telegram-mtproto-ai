"""main.py initialize() 的日志配置步骤(Stage5 收尾,从 initialize 原样迁出)。

setup_logging(assistant, log_config): 按 config.logging 配置 root logger + file handler
(含钉 WARNING 防第三方刷屏、src.* 业务 INFO 落盘)。行为不变。
"""
from __future__ import annotations

import logging
import os
import sys


def setup_logging(assistant, log_config: dict) -> None:
    if log_config:
        log_file = log_config.get("file")
        log_level = log_config.get("level", "INFO")
        console_output = log_config.get("console_output", True)

        # 设置日志记录器级别
        level = getattr(logging, log_level.upper(), logging.INFO)
        assistant.logger.setLevel(level)

        # 重新配置日志记录器
        assistant.logger.handlers.clear()

        # 控制台处理器（强制 UTF-8，避免 GBK 编码 emoji 失败）
        if console_output:
            _utf8_stdout = open(sys.stdout.fileno(), mode='w',
                                encoding='utf-8', errors='replace',
                                closefd=False)
            console_handler = logging.StreamHandler(_utf8_stdout)
            console_handler.setLevel(level)
            console_formatter = logging.Formatter(
                '[%(asctime)s] [%(levelname)s] %(name)s: %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
            )
            console_handler.setFormatter(console_formatter)
            assistant.logger.addHandler(console_handler)

        # 文件处理器（RotatingFileHandler 自动轮转）
        if log_file:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            from logging.handlers import RotatingFileHandler
            max_bytes = int(log_config.get("max_size_mb", 10)) * 1024 * 1024
            backup_count = int(log_config.get("backup_count", 5))
            file_handler = RotatingFileHandler(
                log_file, maxBytes=max_bytes, backupCount=backup_count,
                encoding='utf-8',
            )
            file_handler.setLevel(level)
            file_formatter = logging.Formatter(
                '[%(asctime)s] [%(levelname)s] %(name)s: %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
            )
            file_handler.setFormatter(file_formatter)
            assistant.logger.addHandler(file_handler)
            # 防止 ai_chat_assistant 消息被 root handler 再写一次（duplicate）
            assistant.logger.propagate = False
            # ★ 让非 ai_chat_assistant 家族的 logger（如 src.integrations.messenger_rpa.*）
            # 也能落盘到 app.log；尤其是运行时告警、异常追踪
            try:
                root_logger = logging.getLogger()
                # 避免对 root 造成过度 verbose，最低仍设为 WARNING
                root_level = max(level, logging.WARNING)
                if root_logger.level > root_level or root_logger.level == 0:
                    root_logger.setLevel(root_level)
                # 避免重复添加（热重启场景）
                have_same = any(
                    isinstance(h, RotatingFileHandler)
                    and getattr(h, "baseFilename", "") ==
                    getattr(file_handler, "baseFilename", "")
                    for h in root_logger.handlers
                )
                if not have_same:
                    root_logger.addHandler(file_handler)
            except Exception:
                pass
            # ★★ src.* 命名空间的 INFO 也要落盘（2026-07-12 排障盲区修复）：
            # root 钉在 WARNING 防第三方库（httpx/uvicorn/pyrogram）刷屏，代价是
            # 本仓 src.* 业务模块的 INFO（「配置热重载完成」「入站翻译超时」
            # 「backfill 消化」…）在 app.log 全体隐身——线上行为无从追溯。
            # 单独给 "src" logger 挂同一 file handler（幂等/防重复行语义见模块单测）。
            try:
                from src.utils.log_setup import attach_src_file_handler
                attach_src_file_handler(file_handler, level=level)
            except Exception:
                pass

        assistant.logger.info(f"日志已重新配置: level={log_level}, file={log_file}")
