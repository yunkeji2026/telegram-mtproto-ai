"""日志装配助手 — src.* 命名空间落盘（2026-07-12 排障盲区修复）。

背景：main.py 的日志配置把 root 钉在 WARNING（防 httpx/uvicorn/pyrogram 等三方库
刷屏），代价是本仓 ``src.*`` 业务模块的 INFO（「配置热重载完成」「入站自动翻译超时」
「backfill 消化」…）在 app.log **全体隐身**——当天排障时被「无日志=没触发」的假信号
误导了三轮。修法：单独给 ``"src"`` logger 挂同一个 file handler（INFO 起落盘），
``propagate=False`` 防 WARNING 再经 root 的 handler 重复写一行；三方库不受影响。

抽成独立模块的原因：main.py 的 initialize() 无法单测（拉起整个 assistant），
这里的幂等/不重复/隔离语义值得被测试钉住。
"""

from __future__ import annotations

import logging


def attach_src_file_handler(file_handler: logging.Handler,
                            level: int = logging.INFO) -> logging.Logger:
    """给 ``src`` 命名空间 logger 挂 file handler（幂等，返回该 logger）。

    - ``src.*`` 的 ``level`` 起（默认 INFO）落盘到与主日志同一文件；
    - ``propagate=False``：防同一条记录再经 root 的同文件 handler 写重复行；
    - 幂等：同一 baseFilename 的 handler 不重复挂（热重启/重配置场景）。
    """
    src_logger = logging.getLogger("src")
    src_logger.setLevel(level)
    src_logger.propagate = False
    base = getattr(file_handler, "baseFilename", None)
    have_same = any(
        getattr(h, "baseFilename", None) == base and base is not None
        for h in src_logger.handlers
    )
    if not have_same:
        src_logger.addHandler(file_handler)
    return src_logger
