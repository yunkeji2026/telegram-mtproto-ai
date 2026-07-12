"""main.py web 管理后台的启动编排（Stage 2 拆分目标）。

2026-07-12 Stage 2 起，把 initialize() 内联的 FastAPI web 装配/启动逐簇迁到这里，
把闭包捕获的 self.* 显式化为 assistant 参数。首簇：web 服务线程启动。
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any

from src.utils.net_helpers import is_bind_address_in_use_error


def start_web_server_thread(assistant: Any, server: Any, web_host: str, web_port: int) -> threading.Thread:
    """在独立线程 + 独立 event loop 里跑 uvicorn server，避免与主 loop 抢占。

    从 main.py 的 initialize() 原样抽出（行为不变）：主 loop 上的同步阻塞
    （SQLite 写、BM25 全表扫描）不再卡 web 请求。绑定失败只告警、不挡启动。
    """
    def _run_web_in_thread():
        try:
            web_loop = asyncio.new_event_loop()
            assistant._web_loop = web_loop
            asyncio.set_event_loop(web_loop)
            try:
                web_loop.run_until_complete(server.serve())
            finally:
                try:
                    web_loop.close()
                except Exception:
                    pass
        except OSError as e:
            if is_bind_address_in_use_error(e):
                assistant.logger.warning(
                    "Web 管理后台未启动: 端口 %s 已被占用（通常为先前未退出的本程序实例）。"
                    "请先结束占用进程: taskkill /F /IM python.exe 或修改 config.yaml 中 web_admin.port",
                    web_port,
                )
            else:
                assistant.logger.warning("Web 管理后台启动失败: %s", e)
        except Exception as ex:
            assistant.logger.warning("Web 管理后台启动跳过: %s", ex)

    web_thread = threading.Thread(
        target=_run_web_in_thread,
        name="web_admin_thread",
        daemon=True,
    )
    web_thread.start()
    return web_thread
