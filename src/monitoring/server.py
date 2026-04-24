"""
监控 REST API：健康、指标、日志尾读、配置摘要。
供前端或其他服务通过 HTTP 对接，见 docs/MONITORING_API_SPEC.md。
"""

import logging
import os
from pathlib import Path

from src.monitoring.metrics_store import get_metrics_store

_logger = logging.getLogger("MonitoringAPI")
_app = None


def create_app(assistant_ref=None, auth_token: str = ""):
    try:
        from fastapi import FastAPI, Request, Depends, HTTPException
        from fastapi.middleware.cors import CORSMiddleware
    except ImportError:
        raise ImportError("监控 API 需要安装: pip install fastapi uvicorn")

    app = FastAPI(title="Telegram MTProto AI 监控", version="1.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    store = get_metrics_store()
    if assistant_ref is not None:
        store.set_assistant_ref(assistant_ref)

    def _require_auth(request: Request):
        """FastAPI 依赖注入：无 token 配置时放行，否则校验 Bearer / query param"""
        if not auth_token:
            return
        auth_h = request.headers.get("Authorization", "")
        if auth_h == f"Bearer {auth_token}":
            return
        if request.query_params.get("token", "") == auth_token:
            return
        raise HTTPException(status_code=401, detail="Unauthorized")

    @app.get("/api/health", dependencies=[Depends(_require_auth)])
    def api_health():
        return {
            "status": store.status(),
            "telegram_connected": store.telegram_connected(),
            "ai_healthy": store.ai_healthy(),
            "ai_consecutive_errors": store._ai_consecutive_errors,
            "circuit_breaker": store._cb_state,
            "uptime_seconds": round(store.uptime_seconds(), 1),
            "version": "1.0.0",
        }

    @app.get("/api/metrics", dependencies=[Depends(_require_auth)])
    def api_metrics():
        return store.snapshot()

    @app.get("/api/logs", dependencies=[Depends(_require_auth)])
    def api_logs(tail: int = 100):
        tail = max(1, min(500, tail))
        try:
            config = getattr(assistant_ref, "config", None) if assistant_ref else None
            if config and hasattr(config, "get_logging_config"):
                log_path = config.get_logging_config().get("file") or "logs/app.log"
            else:
                log_path = "logs/app.log"
            path = Path(log_path)
            if not path.is_absolute():
                path = Path.cwd() / path
            if not path.exists():
                return {"lines": []}
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            return {"lines": lines[-tail:]}
        except Exception as e:
            _logger.warning("读取日志文件失败: %s", e)
            return {"lines": []}

    @app.get("/api/config/summary", dependencies=[Depends(_require_auth)])
    def api_config_summary():
        try:
            config = getattr(assistant_ref, "config", None) if assistant_ref else None
            if not config:
                return _default_config_summary()
            tc = config.get_telegram_config()
            sc = config.get_skills_config()
            mon = getattr(config, "config", {}) or {}
            mon = mon.get("monitoring", {})
            return {
                "telegram_session_name": (tc.get("session_name") or "—"),
                "ai_model": config.get_ai_config().get("model", "—"),
                "skills_enabled": sc.get("enabled", []),
                "monitoring_enabled": mon.get("enabled", True),
                "metrics_port": mon.get("metrics_port", 9090),
            }
        except Exception as e:
            _logger.warning("读取配置摘要失败: %s", e)
            return _default_config_summary()

    @app.get("/api/metrics/prometheus", dependencies=[Depends(_require_auth)])
    def api_metrics_prometheus():
        from fastapi.responses import PlainTextResponse
        s = store.snapshot()
        sa = s.get("startup_advisories") or {}
        _sa_total = int(sa.get("total") or 0)
        _sa_warn = int(sa.get("warnings") or 0)
        _al = sa.get("audit_logged_warnings")
        _sa_audit = -1 if _al is None else int(_al)
        lines = [
            "# HELP tg_bot_messages_received Total messages received",
            "# TYPE tg_bot_messages_received counter",
            f"tg_bot_messages_received {s['messages_received']}",
            "# HELP tg_bot_messages_replied Total messages replied",
            "# TYPE tg_bot_messages_replied counter",
            f"tg_bot_messages_replied {s['messages_replied']}",
            "# HELP tg_bot_api_calls Total AI API calls",
            "# TYPE tg_bot_api_calls counter",
            f"tg_bot_api_calls {s['api_calls']}",
            "# HELP tg_bot_errors_total Total errors",
            "# TYPE tg_bot_errors_total counter",
            f"tg_bot_errors_total {s['errors_count']}",
            "# HELP tg_bot_response_time_avg_ms Average response time",
            "# TYPE tg_bot_response_time_avg_ms gauge",
            f"tg_bot_response_time_avg_ms {s['response_time_avg_ms']}",
            "# HELP tg_bot_response_time_p99_ms P99 response time",
            "# TYPE tg_bot_response_time_p99_ms gauge",
            f"tg_bot_response_time_p99_ms {s['response_time_p99_ms']}",
            "# HELP tg_bot_queue_size Current message queue size",
            "# TYPE tg_bot_queue_size gauge",
            f"tg_bot_queue_size {s['queue_size']}",
            "# HELP tg_bot_queue_drops Total dropped messages",
            "# TYPE tg_bot_queue_drops counter",
            f"tg_bot_queue_drops {s.get('queue_drops', 0)}",
            "# HELP tg_bot_fallback_replies Total fallback replies",
            "# TYPE tg_bot_fallback_replies counter",
            f"tg_bot_fallback_replies {s['reply_quality']['fallback_count']}",
            "# HELP tg_bot_truncated_replies Total truncated replies",
            "# TYPE tg_bot_truncated_replies counter",
            f"tg_bot_truncated_replies {s['reply_quality']['truncated_count']}",
            "# HELP tg_bot_uptime_seconds Uptime in seconds",
            "# TYPE tg_bot_uptime_seconds gauge",
            f"tg_bot_uptime_seconds {round(store.uptime_seconds(), 1)}",
            "# HELP tg_bot_active_tasks Current processing tasks",
            "# TYPE tg_bot_active_tasks gauge",
            f"tg_bot_active_tasks {s.get('active_tasks', 0)}",
            "# HELP tg_bot_concurrency_limit Max concurrent tasks",
            "# TYPE tg_bot_concurrency_limit gauge",
            f"tg_bot_concurrency_limit {s.get('concurrency_limit', 0)}",
            "# HELP tg_bot_startup_advisory_total Startup config advisory events (last process)",
            "# TYPE tg_bot_startup_advisory_total gauge",
            f"tg_bot_startup_advisory_total {_sa_total}",
            "# HELP tg_bot_startup_advisory_warnings Startup advisory warning-level count",
            "# TYPE tg_bot_startup_advisory_warnings gauge",
            f"tg_bot_startup_advisory_warnings {_sa_warn}",
            "# HELP tg_bot_startup_advisory_audit_logged Warnings written to audit (-1 if n/a)",
            "# TYPE tg_bot_startup_advisory_audit_logged gauge",
            f"tg_bot_startup_advisory_audit_logged {_sa_audit}",
        ]
        for layer, count in s.get("trigger_layers", {}).items():
            lines.append(f'tg_bot_trigger_layer{{layer="{layer}"}} {count}')
        if s.get("trigger_layers"):
            lines.insert(-1, "# HELP tg_bot_trigger_layer Trigger layer counts")
            lines.insert(-1, "# TYPE tg_bot_trigger_layer counter")
        return PlainTextResponse("\n".join(lines) + "\n",
                                  media_type="text/plain; version=0.0.4; charset=utf-8")

    def _default_config_summary():
        return {
            "telegram_session_name": "—",
            "ai_model": "—",
            "skills_enabled": [],
            "monitoring_enabled": True,
            "metrics_port": 9090,
        }

    return app


def run_server(host: str = "127.0.0.1", port: int = 9090,
               assistant_ref=None, auth_token: str = ""):
    """在调用方线程中运行（通常由 main 在单独线程中调用）。"""
    from src.utils.net_helpers import is_bind_address_in_use_error

    import uvicorn
    app = create_app(assistant_ref, auth_token=auth_token)
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    except OSError as e:
        if is_bind_address_in_use_error(e):
            _logger.warning(
                "监控 API 未启动: 端口 %s 已被占用（通常为先前未退出的本程序实例）。"
                "请先结束占用进程或修改 config.yaml 中 monitoring.metrics_port",
                port,
            )
        else:
            _logger.warning("监控 API 启动失败: %s", e)
