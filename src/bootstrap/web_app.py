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


def make_api_auth(web_app: Any):
    """构造 API 鉴权依赖：优先 admin 的 api_auth（登录校验 + 坐席白名单），
    回退 require_role('line_rpa')。参数带 Request 注解，避免 FastAPI 误判为 query 参数。

    从 initialize() 抽出并去重：原 _drafts_api_auth / _contacts_api_auth 逻辑一致。
    """
    from starlette.requests import Request

    def _api_auth(request: Request):
        _fn = getattr(web_app.state, "api_auth", None)
        if _fn is not None:
            _fn(request)
        elif hasattr(web_app.state, "require_role"):
            web_app.state.require_role(request, "line_rpa")

    return _api_auth


def start_monitoring_thread(assistant: Any):
    """按配置启动监控 API 后台线程（供前端对接）。从 initialize() 原样抽出（行为不变）。

    monitoring.enabled=false 时直接跳过。绑定失败只告警、不挡启动。返回线程或 None。
    """
    mon = getattr(assistant.config, "config", {}) or {}
    mon = mon.get("monitoring", {})
    if not mon.get("enabled", True):
        return None
    try:
        port = int(mon.get("metrics_port", 9090))
        from src.monitoring.server import run_server
        _web_cfg = assistant.config.config.get("web_admin", {})
        mon_token = mon.get("auth_token") or _web_cfg.get("auth_token", "")
        t = threading.Thread(
            target=run_server,
            kwargs={"host": "127.0.0.1", "port": port,
                    "assistant_ref": assistant, "auth_token": mon_token},
            daemon=True,
        )
        t.start()
        assistant._monitor_thread = t
        assistant.logger.info(
            "监控 API 线程已启动，正在绑定 127.0.0.1:%s（若端口被占用将在线程内失败，见日志）",
            port,
        )
        return t
    except Exception as ex:
        assistant.logger.warning(f"监控 API 启动跳过: {ex}")
        return None
