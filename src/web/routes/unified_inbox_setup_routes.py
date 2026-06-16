"""统一收件箱——渠道接入向导路由域（P1-1）。

给后台管理员的引导式接入：选渠道 → 看「还缺什么」→ 填关键凭证（即时校验）→
写入凭证 overlay（config.local.yaml，保住主配置注释）→ 交棒现有扫码登录流程。

复用：
- ``channel_setup.channel_status`` 出每渠道现状（密钥打码回显）；
- ``ConfigManager.save_channel_credentials`` 落盘 overlay 并即时生效 + 自检；
- 现有 ``/api/platforms/{platform}/login/*`` 完成扫码（本域不重复实现）。

``register_setup_routes(app, *, api_auth, config_manager)`` 挂 ``/api/setup/*``（管理员）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import Request

from src.web.routes.unified_inbox_auth import _require_supervisor
from src.web.routes.unified_inbox_services import _inbox_store

logger = logging.getLogger(__name__)


def _count_online_agents(request: Request) -> int:
    """近 120s 内 status=online 的坐席数（上线清单「坐席在线」信号）。"""
    try:
        inbox = _inbox_store(request)
        if inbox is None or not hasattr(inbox, "list_agent_presence"):
            return 0
        rows = inbox.list_agent_presence(active_within_sec=120)
        return sum(1 for r in rows if str(r.get("status") or "") == "online")
    except Exception:
        logger.debug("统计在线坐席失败（已忽略）", exc_info=True)
        return 0


def _kb_readiness_for(config_manager) -> dict:
    """取 KB 冷启动现状（无 store 时返回不可用）。"""
    try:
        from src.utils.kb_registry import get_kb_store
        from src.utils.kb_starter import kb_readiness
        kb = get_kb_store(config_manager, require_exists=True)
        return kb_readiness(kb)
    except Exception:
        logger.debug("读取 KB readiness 失败（已忽略）", exc_info=True)
        return {"available": False, "is_cold": True, "enabled_entries": 0}


def register_setup_routes(app, *, api_auth, config_manager=None) -> None:
    """挂载渠道接入向导端点（/api/setup/channels[/{channel}]）。"""

    @app.get("/api/setup/channels")
    async def api_setup_channels(request: Request):
        """所有渠道的接入现状（启用/缺项/字段填写状态 + 总体完成度）。"""
        api_auth(request)
        _require_supervisor(request)
        from src.utils.channel_setup import channel_status
        config = getattr(config_manager, "config", None) or {}
        channels = channel_status(config)
        ready = sum(1 for c in channels if c["ready"])
        return {"ok": True, "channels": channels,
                "ready_count": ready, "total": len(channels)}

    @app.get("/api/setup/checklist")
    async def api_setup_checklist(request: Request):
        """上线自检清单：AI/渠道/配置/知识库/坐席 就绪信号 + 总体红绿灯。"""
        api_auth(request)
        _require_supervisor(request)
        from src.utils.channel_setup import channel_status
        from src.utils.config_check import check_config
        from src.utils.golive import build_checklist
        config = getattr(config_manager, "config", None) or {}
        channels = channel_status(config)
        errors = warns = 0
        try:
            issues = check_config(
                config, config_path=getattr(config_manager, "config_path", None))
            errors = sum(1 for i in issues if i.severity == "error")
            warns = sum(1 for i in issues if i.severity == "warn")
        except Exception:
            logger.debug("清单内配置自检失败（已忽略）", exc_info=True)
        return build_checklist(
            config=config,
            channel_statuses=channels,
            config_errors=errors,
            config_warnings=warns,
            kb_ready=_kb_readiness_for(config_manager),
            online_agents=_count_online_agents(request),
        )

    @app.post("/api/setup/channels/{channel}")
    async def api_setup_channel_save(channel: str, request: Request):
        """保存某渠道凭证到 overlay 并即时生效；返回该渠道最新现状 + 自检问题。"""
        api_auth(request)
        _require_supervisor(request)
        if config_manager is None:
            return {"ok": False, "detail": "config_manager 不可用"}
        # C0-3：套餐渠道 gating —— enforce 开且该渠道不在授权范围时拒绝接入
        try:
            from src.licensing import channel_allowed, get_license_manager

            _lic = get_license_manager().status()
            if not channel_allowed(_lic, str(channel).lower()):
                return {
                    "ok": False,
                    "error": "channel_not_licensed",
                    "detail": f"当前套餐（{_lic.plan}）未包含「{channel}」渠道，请升级套餐。",
                }
        except Exception:
            logger.debug("渠道 gating 检查跳过（已忽略）", exc_info=True)
        try:
            body: Dict[str, Any] = await request.json()
        except Exception:
            body = {}
        values = dict((body or {}).get("values") or {})
        ok, msg, issues = config_manager.save_channel_credentials(channel, values)
        if not ok:
            return {"ok": False, "detail": msg}
        from src.utils.channel_setup import channel_status
        status = next(
            (c for c in channel_status(config_manager.config or {})
             if c["id"] == str(channel).lower()), None)
        # 只回与该渠道相关的自检问题（按 enable_key/字段前缀粗筛）
        prefix = str(channel).lower()
        rel = [
            {"severity": i.severity, "path": i.path, "message": i.message}
            for i in issues
            if prefix in i.path or (status and any(
                f["key"] in i.path for f in status.get("fields", [])))
        ]
        return {"ok": True, "detail": msg, "channel": status, "issues": rel}
