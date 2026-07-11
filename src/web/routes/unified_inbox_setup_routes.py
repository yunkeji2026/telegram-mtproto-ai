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
from src.web.web_i18n import tr

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


def _session_present(request: Request) -> bool:
    """请求是否携带已登录 session（区别于桌面壳主进程的纯 Bearer 调用）。"""
    try:
        if "session" in request.scope:
            s = request.session
            return bool(s.get("user_id") or s.get("auth"))
    except Exception:
        pass
    return False


def _require_supervisor_or_shell(request: Request) -> None:
    """AI 凭证端点守卫：session 用户须主管角色；纯 Bearer（桌面壳主进程，
    api_auth 已验 admin token = master 等价）放行。"""
    if _session_present(request):
        _require_supervisor(request)


async def reload_ai_runtime(app, config_manager) -> bool:
    """P0-1：AI 凭证落盘后热重建 AIClient 并换绑运行中服务（best-effort，绝不抛）。

    覆盖：``app.state.ai_client`` / ``translation_service``（含路由内 AIEngine）/
    ``chat_assistant_service`` / ``skill_manager.ai_client``——即「填 Key → 翻译/智能
    回复生效」主链路免重启。其余在启动期快照旧 client 的子系统（companion worker 等）
    仍需重启进程；初始化失败（key 无效/网络不通）时**不换绑**，保持旧 client。
    返回：新 client 连接自检是否通过（可直接当「翻译就绪」绿灯）。
    """
    try:
        from src.ai.ai_client import AIClient
        client = AIClient(config_manager)
        if not bool(await client.initialize()):
            return False
        app.state.ai_client = client
        svc = getattr(app.state, "translation_service", None)
        if svc is not None and hasattr(svc, "rebind_ai_client"):
            svc.rebind_ai_client(client)
        cas = getattr(app.state, "chat_assistant_service", None)
        if cas is not None and hasattr(cas, "ai_client"):
            cas.ai_client = client
        sm = getattr(app.state, "skill_manager", None)
        if sm is not None and hasattr(sm, "ai_client"):
            sm.ai_client = client
        return True
    except Exception:
        logger.warning("AI runtime 热重建失败（已忽略；重启后生效）", exc_info=True)
        return False


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

    @app.get("/api/setup/companion-preflight")
    async def api_setup_companion_preflight(request: Request):
        """Phase N：真号扫码陪聊上线前自检——开关一致性 + 反封号护栏就绪红绿灯。

        把 N-Line checklist §1/§4 的开关一致性固化成可机检的红绿灯，operator 扫码前先看。
        """
        api_auth(request)
        _require_supervisor(request)
        from src.ops.companion_preflight import build_companion_preflight
        config = getattr(config_manager, "config", None) or {}
        return build_companion_preflight(config)

    @app.get("/api/setup/ai")
    async def api_setup_ai_status(request: Request):
        """AI 大模型配置现状（key 打码回显；供首启向导/接入向导预填）。"""
        api_auth(request)
        _require_supervisor_or_shell(request)
        from src.utils.golive import _is_placeholder
        config = getattr(config_manager, "config", None) or {}
        ai = config.get("ai") or {}
        key = str(ai.get("api_key") or "")
        configured = not _is_placeholder(key)
        masked = (key[:4] + "…" + key[-4:]) if len(key) > 12 else ("…" if key.strip() else "")
        return {
            "ok": True,
            "configured": configured,
            "provider": str(ai.get("provider") or ""),
            "base_url": str(ai.get("base_url") or ""),
            "model": str(ai.get("model") or ""),
            "api_key_masked": masked,
        }

    @app.post("/api/setup/ai-key")
    async def api_setup_ai_key_save(request: Request):
        """保存 AI 凭证到 overlay（config.local.yaml）并热重建 AI 运行时（P0-1 A2）。

        写 overlay 而非主 config.yaml：保住注释/结构，密钥不进 git 跟踪文件。
        成功后返回 ``ai_ready``（新 client 连接自检结果）——即「翻译就绪」绿灯。
        """
        api_auth(request)
        _require_supervisor_or_shell(request)
        if config_manager is None:
            return {"ok": False, "detail": tr(request, "err.svc.config_manager_not_ready")}
        try:
            body: Dict[str, Any] = await request.json()
        except Exception:
            body = {}
        from src.utils.golive import _is_placeholder
        api_key = str((body or {}).get("api_key") or "").strip()
        if _is_placeholder(api_key):
            return {"ok": False, "detail": tr(request, "err.auth.api_key_required")}
        values = {
            "api_key": api_key,
            "provider": (body or {}).get("provider"),
            "base_url": (body or {}).get("base_url"),
            "model": (body or {}).get("model"),
        }
        ok, msg = config_manager.save_ai_credentials(values)
        if not ok:
            return {"ok": False, "detail": tr(request, "err.setup.ai_save_failed", reason=msg)}
        ai_ready = await reload_ai_runtime(request.app, config_manager)
        return {
            "ok": True,
            "detail": tr(request, "setup.ai.saved"),
            "ai_ready": bool(ai_ready),
            "provider": str(((config_manager.config or {}).get("ai") or {}).get("provider") or ""),
        }

    @app.post("/api/setup/channels/{channel}")
    async def api_setup_channel_save(channel: str, request: Request):
        """保存某渠道凭证到 overlay 并即时生效；返回该渠道最新现状 + 自检问题。"""
        api_auth(request)
        _require_supervisor(request)
        if config_manager is None:
            return {"ok": False, "detail": tr(request, "err.svc.config_manager_not_ready")}
        # C0-3：套餐渠道 gating —— enforce 开且该渠道不在授权范围时拒绝接入
        try:
            from src.licensing import channel_allowed, get_license_manager

            _lic = get_license_manager().status()
            if not channel_allowed(_lic, str(channel).lower()):
                return {
                    "ok": False,
                    "error": "channel_not_licensed",
                    "detail": tr(request, "err.ws.plan_channel_not_included", plan=_lic.plan, channel=channel),
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
