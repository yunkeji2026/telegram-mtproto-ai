"""统一收件箱——转化标记 / 批量触达路由域（巨石拆分 slice 32）。

把 ``register_unified_inbox_routes`` 巨型闭包中连续的「成交闭环 + 分组触达」子域
整体外移为 ``register_conversion_outreach_routes(app, *, api_auth)``，由主 register
在**原位置**调用：

- ``unified-inbox/mark-conversion``：人工标记 BONDED/CONVERTED（FSM 终点可达）
- ``unified-inbox/outreach/preview``：分组批量触达 dry-run 预览
- ``unified-inbox/outreach/execute``：分组批量触达真实发送
- ``unified-inbox/outreach/batch``：批次回执统计 + 回复率

端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫 + slice 32 端点契约断言）。

依赖全部朝下：services.(_inbox_store/_contacts_store)、auth._agent_from_request、
helpers.FUNNEL_STAGE_LABELS、aggregate._INBOX_ADAPTERS、channel_adapters.send_via_adapters。
只收 api_auth 一个参数。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import Depends, Request

from src.inbox.channel_adapters import send_via_adapters
from src.web.routes.unified_inbox_aggregate import _INBOX_ADAPTERS
from src.web.routes.unified_inbox_auth import _agent_from_request
from src.web.routes.unified_inbox_helpers import FUNNEL_STAGE_LABELS
from src.web.routes.unified_inbox_services import _contacts_store, _inbox_store

logger = logging.getLogger(__name__)


def _outreach_cfg(request: Request) -> dict:
    """读取 config.outreach（三端点共用，默认 {}）。"""
    cm = getattr(request.app.state, "config_manager", None)
    try:
        return dict((getattr(cm, "config", None) or {}).get("outreach") or {})
    except Exception:
        return {}


def register_conversion_outreach_routes(app, *, api_auth) -> None:
    """挂载转化标记 + 批量触达端点（mark-conversion / outreach preview·execute·batch）。"""

    @app.post("/api/unified-inbox/mark-conversion")
    async def api_unified_inbox_mark_conversion(request: Request, _=Depends(api_auth)):
        """阶段 E：人工标记会话所属客户为 成交(BONDED)/已转化(CONVERTED)。

        修复"空心漏斗"：此前 gateway 自动流转最高只到 LINE_ENGAGED，BONDED/CONVERTED
        作为终点存在却无任何代码路径写入 → 转化漏斗终点 KPI 永远为 0、不可达。
        本端点让坐席可手动闭环成交，FSM 守卫非法转移、落 stage_change event（记录操作人）。

        body: {conversation_id, stage?(BONDED|CONVERTED), contact_id?, note?}
        需启用 contacts 子系统（config.contacts.enabled）。
        """
        body = await request.json()
        conversation_id = str(body.get("conversation_id") or "")
        target = str(body.get("stage") or "BONDED").strip().upper()
        note = str(body.get("note") or "")
        if target not in ("BONDED", "CONVERTED"):
            return {"ok": False, "reason": "bad_stage",
                    "message": "stage 仅支持 BONDED(成交) 或 CONVERTED(已转化)"}

        cstore = _contacts_store(request)
        if cstore is None:
            return {"ok": False, "reason": "contacts_disabled",
                    "message": "客户旅程子系统未启用（config.contacts.enabled）"}

        # 解析 journey：优先 body.contact_id，其次会话 meta 关联的 contact_id
        contact_id = str(body.get("contact_id") or "")
        if not contact_id and conversation_id:
            istore = _inbox_store(request)
            try:
                meta = istore.get_conv_meta(conversation_id) if istore else {}
                contact_id = str((meta or {}).get("contact_id") or "")
            except Exception:
                contact_id = ""
        if not contact_id:
            return {"ok": False, "reason": "no_contact",
                    "message": "该会话尚未关联客户，无法标记成交"}

        try:
            journey = cstore.get_journey_by_contact(contact_id)
        except Exception:
            journey = None
        if journey is None:
            return {"ok": False, "reason": "no_journey",
                    "message": "未找到该客户的旅程记录"}

        try:
            agent_id, _agent_name = _agent_from_request(request)
        except Exception:
            agent_id = ""
        from src.contacts.journey_fsm import transit as _fsm_transit_fn
        ok = _fsm_transit_fn(
            cstore, journey_id=journey.journey_id, to_stage=target,
            payload={"manual": True, "by": agent_id or "agent", "note": note},
        )
        if not ok:
            try:
                cur = cstore.get_journey(journey.journey_id)
                cur_stage = cur.funnel_stage if cur else journey.funnel_stage
            except Exception:
                cur_stage = journey.funnel_stage
            return {
                "ok": False, "reason": "transition_blocked",
                "current_stage": cur_stage,
                "current_stage_label": FUNNEL_STAGE_LABELS.get(cur_stage, cur_stage),
                "message": f"不能从「{FUNNEL_STAGE_LABELS.get(cur_stage, cur_stage)}」"
                           f"直接标记为「{FUNNEL_STAGE_LABELS.get(target, target)}」",
            }
        try:
            j2 = cstore.get_journey(journey.journey_id)
            new_stage = j2.funnel_stage if j2 else target
        except Exception:
            new_stage = target
        return {
            "ok": True,
            "funnel_stage": new_stage,
            "funnel_stage_label": FUNNEL_STAGE_LABELS.get(new_stage, new_stage),
        }

    @app.post("/api/unified-inbox/outreach/preview")
    async def api_unified_inbox_outreach_preview(request: Request, _=Depends(api_auth)):
        """P61-3：分组批量触达 dry-run 预览（只读不发）。

        body: {platform?, tags_any?[], rel_stages?[], min_silent_days?, max_silent_days?,
               exclude_archived?, limit?}
        返回命中人数、可触达名单、跳过原因（cooldown/account_cap）、每账号分布、预计耗时。
        """
        from src.inbox.outreach_planner import OutreachFilters, OutreachPlanner

        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "reason": "no_store", "message": "持久层未挂载"}

        body = await request.json()
        ocfg = _outreach_cfg(request)

        filters = OutreachFilters(
            platform=str(body.get("platform") or ""),
            tags_any=[str(t) for t in (body.get("tags_any") or []) if str(t).strip()],
            rel_stages=[str(s) for s in (body.get("rel_stages") or []) if str(s).strip()],
            min_silent_days=float(body.get("min_silent_days") or 0),
            max_silent_days=float(body.get("max_silent_days") or 0),
            exclude_archived=bool(body.get("exclude_archived", True)),
            limit=int(body.get("limit") or 500),
        )
        planner = OutreachPlanner(
            store,
            limiter=getattr(request.app.state, "account_limiter", None),
            cooldown_days=float(ocfg.get("cooldown_days", 14)),
            per_send_seconds=float(ocfg.get("per_send_seconds", 8)),
            default_account_cap=int(ocfg.get("default_account_cap", 30)),
        )
        plan = planner.build_plan(filters)
        out = plan.to_dict()
        out["ok"] = True
        return out

    @app.post("/api/unified-inbox/outreach/execute")
    async def api_unified_inbox_outreach_execute(request: Request, _=Depends(api_auth)):
        """P61-4：分组批量触达执行（真实发送）。需 feature-flag + 二次确认。

        body: {filters{}, template, confirm:true, max_send?, batch_id?}
        服务端按 filters 重建 plan（不信任客户端名单）→ 真实扣配额 → RPA 发送 →
        落回执。受 config.outreach.enabled 门禁与 config.outreach.max_batch 硬上限保护。
        """
        from src.inbox.outreach_executor import OutreachExecutor
        from src.inbox.outreach_planner import OutreachFilters, OutreachPlanner

        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "reason": "no_store", "message": "持久层未挂载"}

        ocfg = _outreach_cfg(request)
        if not ocfg.get("enabled", False):
            return {"ok": False, "reason": "outreach_disabled",
                    "message": "批量触达未启用（config.outreach.enabled）"}

        body = await request.json()
        if body.get("confirm") is not True:
            return {"ok": False, "reason": "confirm_required",
                    "message": "执行批量发送需显式 confirm=true"}
        template = str(body.get("template") or "").strip()
        if not template:
            return {"ok": False, "reason": "empty_template", "message": "消息模板不能为空"}

        fb = body.get("filters") or {}
        filters = OutreachFilters(
            platform=str(fb.get("platform") or ""),
            tags_any=[str(t) for t in (fb.get("tags_any") or []) if str(t).strip()],
            rel_stages=[str(s) for s in (fb.get("rel_stages") or []) if str(s).strip()],
            min_silent_days=float(fb.get("min_silent_days") or 0),
            max_silent_days=float(fb.get("max_silent_days") or 0),
            exclude_archived=bool(fb.get("exclude_archived", True)),
            limit=int(fb.get("limit") or 500),
        )
        limiter = getattr(request.app.state, "account_limiter", None)
        planner = OutreachPlanner(
            store, limiter=limiter,
            cooldown_days=float(ocfg.get("cooldown_days", 14)),
            per_send_seconds=float(ocfg.get("per_send_seconds", 8)),
            default_account_cap=int(ocfg.get("default_account_cap", 30)),
        )
        plan = planner.build_plan(filters)

        hard_cap = max(1, int(ocfg.get("max_batch", 50)))
        req_max = int(body.get("max_send") or 0)
        max_send = min(hard_cap, req_max) if req_max > 0 else hard_cap

        async def _send_fn(target, text):
            return await send_via_adapters(
                request, target.platform, target.account_id, target.chat_key,
                text, _INBOX_ADAPTERS,
            )

        executor = OutreachExecutor(
            store, _send_fn, limiter=limiter,
            per_send_seconds=float(ocfg.get("per_send_seconds", 8)),
            sleep_fn=asyncio.sleep,
        )
        result = await executor.execute(
            plan.eligible, template,
            batch_id=str(body.get("batch_id") or ""), max_send=max_send,
        )
        result["planned_eligible"] = len(plan.eligible)
        return result

    @app.get("/api/unified-inbox/outreach/batch")
    async def api_unified_inbox_outreach_batch(
        request: Request, batch_id: str = "", response_window_days: float = 0,
        _=Depends(api_auth),
    ):
        """P61-4/5：查某批次回执统计（成功/失败计数 + P61-5 回复率）。"""
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "reason": "no_store"}
        stats = store.outreach_batch_stats(batch_id)
        ocfg = _outreach_cfg(request)
        win = float(response_window_days) if response_window_days else float(ocfg.get("response_window_days", 7))
        stats["response"] = store.outreach_response_stats(batch_id, response_window_days=win)
        stats["ok"] = True
        return stats
