"""统一收件箱——坐席协作 presence / 会话租约(claims) 路由域（巨石拆分 slice 11）。

把"Phase 5：坐席协作（presence + 会话租约）"这一自包含子域，从
``register_unified_inbox_routes`` 巨型闭包中外移为
``register_workspace_presence_routes(app, *, api_auth, config_manager)``，由主 register
顺序调用。端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫保证）。

依赖：AgentCoordinator / web_funnel_snapshot（workspace 包）、auth._session_agent、
normalizer.conv_id；只朝下依赖，无回 routes 依赖。子注册函数只收 api_auth + config_manager。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import Depends, HTTPException, Request

from src.inbox.normalizer import conv_id
from src.web.routes.unified_inbox_auth import _session_agent
from src.workspace.agent_coordinator import AgentCoordinator, web_funnel_snapshot

logger = logging.getLogger(__name__)


def _online_agent_ids(request: Request, within_sec: int = 120) -> list:
    """近 within_sec 秒内 status=online 的坐席 id（席位强制用）。store 不可用 → []。"""
    try:
        from src.web.routes.unified_inbox_services import _inbox_store
        inbox = _inbox_store(request)
        if inbox is None or not hasattr(inbox, "list_agent_presence"):
            return []
        rows = inbox.list_agent_presence(active_within_sec=within_sec)
        return [str(r.get("agent_id") or "") for r in rows
                if str(r.get("status") or "") == "online"]
    except Exception:
        logger.debug("统计在线坐席失败（席位强制放行）", exc_info=True)
        return []


def _seat_block(request: Request, agent_id: str) -> bool:
    """该坐席上线是否应被授权席位拦截。任何异常 → False（放行，绝不误伤）。"""
    try:
        from src.licensing import get_license_manager, seat_block_on_online
        st = get_license_manager().status()
        return bool(seat_block_on_online(st, _online_agent_ids(request), agent_id))
    except Exception:
        logger.debug("席位强制判定失败（放行）", exc_info=True)
        return False


def register_workspace_presence_routes(app, *, api_auth, config_manager=None) -> None:
    """挂载坐席 presence / 会话租约 / web 漏斗快照端点（/api/workspace/presence|claim|claims|heartbeat）。"""

    # ── Phase 5：坐席协作（presence + 会话租约）────────────────────
    @app.get("/api/workspace/presence")
    async def api_workspace_presence_list(request: Request):
        api_auth(request)
        coord = AgentCoordinator.from_request(request, config_manager)
        return {"ok": True, "agents": coord.list_presence()}

    @app.post("/api/workspace/presence")
    async def api_workspace_presence_set(request: Request, _=Depends(api_auth)):
        body = await request.json()
        status = str(body.get("status") or "online")
        agent = _session_agent(request)
        coord = AgentCoordinator.from_request(request, config_manager)
        # J·席位强制（License enforce）：仅当 licensing.enforce 开 + 授权有限席位时，
        # 新坐席「上线」超额 → 403。enforce 关 / seats=0 → 恒放行（零破坏）。
        if status == "online" and _seat_block(request, agent["agent_id"]):
            raise HTTPException(
                status_code=403,
                detail="seat_limit:活跃坐席已达授权席位上限，请升级套餐或让其他坐席下线",
            )
        row = coord.set_presence(
            agent["agent_id"],
            display_name=str(body.get("display_name") or agent["display_name"]),
            status=status,
        )
        return {"ok": True, "presence": row}

    @app.post("/api/workspace/heartbeat")
    async def api_workspace_heartbeat(request: Request, _=Depends(api_auth)):
        body: Dict[str, Any] = {}
        try:
            raw = await request.json()
            if isinstance(raw, dict):
                body = raw
        except Exception:
            pass
        agent = _session_agent(request)
        coord = AgentCoordinator.from_request(request, config_manager)
        row = coord.heartbeat(
            agent["agent_id"],
            display_name=str(body.get("display_name") or agent["display_name"]),
            status=str(body.get("status") or ""),
        )
        return {"ok": True, "presence": row}

    @app.get("/api/workspace/claims")
    async def api_workspace_claims_list(request: Request):
        api_auth(request)
        coord = AgentCoordinator.from_request(request, config_manager)
        return {"ok": True, "claims": coord.list_claims()}

    @app.post("/api/workspace/claim")
    async def api_workspace_claim(request: Request, _=Depends(api_auth)):
        body = await request.json()
        platform = str(body.get("platform") or "").lower()
        account_id = str(body.get("account_id") or "default")
        chat_key = str(body.get("chat_key") or "")
        conversation_id = str(body.get("conversation_id") or "").strip()
        if not conversation_id:
            if not platform or not chat_key:
                raise HTTPException(400, "conversation_id 或 platform+chat_key 必填")
            conversation_id = conv_id(platform, account_id, chat_key)
        force = bool(body.get("force"))
        agent = _session_agent(request)
        coord = AgentCoordinator.from_request(request, config_manager)
        result = coord.claim(
            conversation_id,
            agent["agent_id"],
            agent_name=agent["display_name"],
            force=force,
        )
        if not result.get("ok"):
            return {"ok": False, **result}
        return {"ok": True, "conversation_id": conversation_id, "claim": result.get("claim")}

    @app.post("/api/workspace/claim/renew")
    async def api_workspace_claim_renew(request: Request, _=Depends(api_auth)):
        body = await request.json()
        conversation_id = str(body.get("conversation_id") or "").strip()
        if not conversation_id:
            platform = str(body.get("platform") or "").lower()
            chat_key = str(body.get("chat_key") or "")
            account_id = str(body.get("account_id") or "default")
            if not platform or not chat_key:
                raise HTTPException(400, "conversation_id 或 platform+chat_key 必填")
            conversation_id = conv_id(platform, account_id, chat_key)
        agent = _session_agent(request)
        coord = AgentCoordinator.from_request(request, config_manager)
        return coord.renew_claim(conversation_id, agent["agent_id"])

    @app.post("/api/workspace/claim/release")
    async def api_workspace_claim_release(request: Request, _=Depends(api_auth)):
        body = await request.json()
        conversation_id = str(body.get("conversation_id") or "").strip()
        if not conversation_id:
            platform = str(body.get("platform") or "").lower()
            chat_key = str(body.get("chat_key") or "")
            account_id = str(body.get("account_id") or "default")
            if not platform or not chat_key:
                raise HTTPException(400, "conversation_id 或 platform+chat_key 必填")
            conversation_id = conv_id(platform, account_id, chat_key)
        force = bool(body.get("force"))
        agent = _session_agent(request)
        coord = AgentCoordinator.from_request(request, config_manager)
        return coord.release_claim(conversation_id, agent["agent_id"], force=force)

    @app.get("/api/workspace/metrics/web-funnel")
    async def api_workspace_web_funnel(request: Request):
        api_auth(request)
        return {"ok": True, "metrics": web_funnel_snapshot(request, config_manager)}
