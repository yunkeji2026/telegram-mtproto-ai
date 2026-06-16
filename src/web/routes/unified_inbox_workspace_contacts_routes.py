"""统一收件箱——坐席工作台联系人/CRM/跟进任务路由域（巨石拆分 slice 12）。

把"Phase 5-5 手动合并/拆分/审核队列 + Phase 6-1 Contact 360 全景 + Phase 6-2 客户列表/CRM
+ 跟进任务"这一内聚子域，从 ``register_unified_inbox_routes`` 巨型闭包中外移为
``register_workspace_contacts_routes(app, *, api_auth, page_auth, templates, config_manager)``，
由主 register 在**原位置**顺序调用。端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫）。

依赖全部朝下（services/auth/context/helpers 已成模块）或来自 register 参数（page_auth/templates/
config_manager），无回 routes 依赖。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from src.web.routes.unified_inbox_auth import _publish_follow_up, _session_agent
from src.web.routes.unified_inbox_context import _build_contact_timeline
from src.web.routes.unified_inbox_helpers import (
    _EVENT_LABELS,
    _PLATFORM_LABELS,
    _fmt_ts,
    FUNNEL_STAGE_LABELS,
)
from src.web.routes.unified_inbox_services import _contacts_gateway, _contacts_store

logger = logging.getLogger(__name__)


def register_workspace_contacts_routes(
    app, *, api_auth, page_auth, templates, config_manager=None,
) -> None:
    """挂载坐席工作台联系人/CRM/跟进任务端点（/api/workspace/contacts*、/contact*、/follow-up*、/my-tasks 等）。"""

    # ── Phase 5-5：坐席手动合并 / 拆分 / 审核队列 ────────────────
    @app.get("/api/workspace/contacts/overview")
    async def api_workspace_contact_overview(
        request: Request,
        platform: str = "",
        account_id: str = "default",
        chat_key: str = "",
    ):
        """当前会话对应 Contact 档案 + 该 Contact 的渠道身份 + 可合并候选。"""
        api_auth(request)
        gw = _contacts_gateway(request)
        store = _contacts_store(request)
        if gw is None or store is None:
            return {"ok": False, "error": "contacts_disabled"}
        ci = store.get_ci_by_external(platform, account_id, chat_key)
        if ci is None:
            return {"ok": True, "contact": None, "candidates": []}
        overview = gw.contact_overview(ci.contact_id)
        candidates = gw.merge_candidates_for(ci.contact_id)
        return {
            "ok": True,
            "current_ci_id": ci.channel_identity_id,
            "contact": overview,
            "candidates": candidates,
        }

    @app.post("/api/workspace/contacts/merge")
    async def api_workspace_contact_merge(request: Request, _=Depends(api_auth)):
        body = await request.json()
        ci_id = str(body.get("ci_id") or "").strip()
        target = str(body.get("target_contact_id") or "").strip()
        if not ci_id or not target:
            raise HTTPException(400, "ci_id 和 target_contact_id 必填")
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        agent = _session_agent(request)
        try:
            ok = gw.manual_merge_identity(
                ci_id=ci_id, target_contact_id=target, operator=agent["agent_id"],
            )
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": bool(ok), "merged": bool(ok), "target_contact_id": target}

    @app.post("/api/workspace/contacts/merge-contact")
    async def api_workspace_contact_merge_contact(request: Request, _=Depends(api_auth)):
        """contact 级合并：把 source 的所有渠道身份并入 target。"""
        body = await request.json()
        source = str(body.get("source_contact_id") or "").strip()
        target = str(body.get("target_contact_id") or "").strip()
        if not source or not target:
            raise HTTPException(400, "source_contact_id 和 target_contact_id 必填")
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        agent = _session_agent(request)
        ok = gw.merge_contacts(
            source_contact_id=source, target_contact_id=target, operator=agent["agent_id"],
        )
        return {"ok": bool(ok), "merged": bool(ok), "target_contact_id": target}

    @app.post("/api/workspace/contacts/split")
    async def api_workspace_contact_split(request: Request, _=Depends(api_auth)):
        body = await request.json()
        ci_id = str(body.get("ci_id") or "").strip()
        if not ci_id:
            raise HTTPException(400, "ci_id 必填")
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        agent = _session_agent(request)
        new_cid = gw.split_identity(ci_id=ci_id, operator=agent["agent_id"])
        if not new_cid:
            return {"ok": False, "error": "nothing_to_split"}
        return {"ok": True, "new_contact_id": new_cid}

    @app.get("/api/workspace/merge-reviews")
    async def api_workspace_merge_reviews(request: Request):
        """待人工裁决的合并候选队列（含两侧档案摘要供对比）。"""
        api_auth(request)
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled", "reviews": []}
        store = _contacts_store(request)
        out: List[Dict[str, Any]] = []
        for rv in gw.list_pending_merge_reviews():
            cand_ci = store.get_channel_identity(rv["candidate_ci_id"]) if store else None
            cand_overview = (
                gw.contact_overview(cand_ci.contact_id) if cand_ci else None
            )
            out.append({
                **rv,
                "candidate": cand_overview,
                "candidate_channel": cand_ci.channel if cand_ci else "",
                "target": gw.contact_overview(rv["target_contact_id"]),
            })
        return {"ok": True, "reviews": out, "count": len(out)}

    @app.post("/api/workspace/merge-reviews/{review_id}")
    async def api_workspace_merge_review_resolve(
        review_id: str, request: Request, _=Depends(api_auth),
    ):
        body = await request.json()
        action = str(body.get("action") or "").lower()
        if action not in ("approve", "reject"):
            raise HTTPException(400, "action 必须是 approve / reject")
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        agent = _session_agent(request)
        if action == "approve":
            ok = gw.approve_merge_review(review_id, resolved_by=agent["agent_id"])
        else:
            ok = gw.reject_merge_review(review_id, resolved_by=agent["agent_id"])
        return {"ok": bool(ok), "action": action, "review_id": review_id}

    # ── Phase 6-1：Contact 360 全景视图 ─────────────────────────
    @app.get("/api/workspace/contacts/search")
    async def api_workspace_contacts_search(request: Request, q: str = "", limit: int = 20):
        """按 名称 / contact_id / 渠道 external_id 搜索 Contact（手动合并目标选择）。"""
        api_auth(request)
        gw = _contacts_gateway(request)
        store = _contacts_store(request)
        if gw is None or store is None:
            return {"ok": False, "error": "contacts_disabled", "contacts": []}
        limit = max(1, min(50, int(limit or 20)))
        contacts, total = store.search_contacts(str(q or "").strip(), limit=limit)
        out = []
        for c in contacts:
            ov = gw.contact_overview(c.contact_id)
            if ov:
                out.append(ov)
        return {"ok": True, "contacts": out, "total": total}

    @app.get("/api/workspace/contact/{contact_id}")
    async def api_workspace_contact_detail(
        contact_id: str, request: Request, msg_limit: int = 60, before_ts: float = 0.0,
    ):
        """Contact 360：聚合档案 + 跨渠道消息时间线 + 事件历史 + 合并候选。

        before_ts>0：分页加载更早消息（仅返回 timeline，前端拼接）。
        """
        api_auth(request)
        gw = _contacts_gateway(request)
        store = _contacts_store(request)
        if gw is None or store is None:
            return {"ok": False, "error": "contacts_disabled"}
        overview = gw.contact_overview(contact_id)
        if overview is None:
            raise HTTPException(404, "contact 不存在")
        msg_limit = max(10, min(200, int(msg_limit or 60)))
        cursor = float(before_ts) if before_ts and before_ts > 0 else None
        timeline = _build_contact_timeline(
            request, overview.get("identities") or [], msg_limit, before_ts=cursor,
        )
        # 翻页请求：只回时间线 + 下一页游标
        next_cursor = timeline[0]["ts"] if (len(timeline) >= msg_limit and timeline) else 0
        if cursor is not None:
            return {"ok": True, "timeline": timeline, "next_cursor": next_cursor,
                    "has_more": bool(next_cursor)}
        journey = store.get_journey_by_contact(contact_id)
        events: List[Dict[str, Any]] = []
        if journey is not None:
            for e in store.list_events(journey.journey_id, limit=40):
                et = e.get("event_type") or e.get("type") or ""
                events.append({
                    "event_type": et,
                    "label": _EVENT_LABELS.get(et, et),
                    "ts": e.get("ts") or 0,
                    "payload": e.get("payload") or {},
                })
        candidates = gw.merge_candidates_for(contact_id)
        return {
            "ok": True,
            "contact": overview,
            "timeline": timeline,
            "next_cursor": next_cursor,
            "has_more": bool(next_cursor),
            "events": events,
            "candidates": candidates,
        }

    @app.get("/workspace/contact/{contact_id}", response_class=HTMLResponse)
    async def workspace_contact_page(
        contact_id: str, request: Request, _=Depends(page_auth),
    ):
        ctx: Dict[str, Any] = {
            "contact_id": contact_id,
            "user_name": request.session.get("username") or "",
            "user_display_name": request.session.get("display_name")
            or request.session.get("username") or "",
        }
        try:
            if config_manager is not None:
                _wa = (config_manager.config or {}).get("web_admin", {}) or {}
                if _wa.get("site_name"):
                    ctx["site_name"] = _wa.get("site_name")
        except Exception:
            pass
        return templates.TemplateResponse(request, "contact360.html", ctx)

    # ── Phase 6-2：客户列表 / CRM 入口 ──────────────────────────
    @app.get("/api/workspace/contacts/list")
    async def api_workspace_contacts_list(
        request: Request,
        q: str = "",
        stage: str = "",
        has_lead: str = "",
        tag: str = "",
        follow_up: str = "",
        limit: int = 30,
        offset: int = 0,
    ):
        """CRM 客户列表：分页 + 阶段/留资/标签/跟进筛选 + 漏斗阶段汇总。"""
        api_auth(request)
        store = _contacts_store(request)
        if store is None:
            return {"ok": False, "error": "contacts_disabled", "contacts": []}
        limit = max(5, min(100, int(limit or 30)))
        offset = max(0, int(offset or 0))
        lead_filter: Optional[bool] = None
        if has_lead in ("1", "true", "yes"):
            lead_filter = True
        elif has_lead in ("0", "false", "no"):
            lead_filter = False
        fu = follow_up if follow_up in ("due", "any") else ""
        rows, total = store.list_contacts_overview(
            q=str(q or "").strip(), stage=str(stage or "").strip(),
            has_lead=lead_filter, tag=str(tag or "").strip(), follow_up=fu,
            limit=limit, offset=offset,
        )
        for r in rows:
            r["funnel_stage_label"] = FUNNEL_STAGE_LABELS.get(
                r.get("funnel_stage") or "", r.get("funnel_stage") or "")
            r["channel_labels"] = [
                _PLATFORM_LABELS.get(c, c) for c in (r.get("channels") or [])
            ]
        try:
            stage_counts = store.count_journeys_by_stage()
        except Exception:
            stage_counts = {}
        try:
            due_count = store.count_due_follow_ups()
        except Exception:
            due_count = 0
        return {
            "ok": True,
            "contacts": rows,
            "total": total,
            "limit": limit,
            "offset": offset,
            "stage_counts": stage_counts,
            "stage_labels": FUNNEL_STAGE_LABELS,
            "due_follow_ups": due_count,
        }

    @app.post("/api/workspace/contact/{contact_id}/crm")
    async def api_workspace_contact_crm(
        contact_id: str, request: Request, _=Depends(api_auth),
    ):
        """保存客户 CRM 字段：备注 / 标签 / 跟进时间。未传的字段不改。"""
        body = await request.json()
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        note = body.get("note")
        tags = body.get("tags")
        if tags is not None and not isinstance(tags, list):
            raise HTTPException(400, "tags 必须是数组")
        fu = body.get("follow_up_at")
        follow_up_at = None
        if fu is not None:
            try:
                follow_up_at = int(fu)
            except (TypeError, ValueError):
                raise HTTPException(400, "follow_up_at 必须是时间戳整数")
        agent = _session_agent(request)
        return gw.update_contact_crm(
            contact_id, note=note, tags=tags, follow_up_at=follow_up_at,
            operator=agent["agent_id"],
        )

    @app.get("/api/workspace/follow-ups")
    async def api_workspace_follow_ups(request: Request, scope: str = "due", limit: int = 50):
        """待跟进客户列表（scope=due 已到期 / any 全部有跟进）+ 到期计数（全部/本人）。"""
        api_auth(request)
        store = _contacts_store(request)
        if store is None:
            return {"ok": False, "error": "contacts_disabled", "contacts": []}
        scope = scope if scope in ("due", "any") else "due"
        rows, total = store.list_contacts_overview(
            follow_up=scope, limit=max(5, min(100, int(limit or 50))),
        )
        for r in rows:
            r["funnel_stage_label"] = FUNNEL_STAGE_LABELS.get(
                r.get("funnel_stage") or "", r.get("funnel_stage") or "")
            r["channel_labels"] = [
                _PLATFORM_LABELS.get(c, c) for c in (r.get("channels") or [])
            ]
        agent = _session_agent(request)
        return {"ok": True, "contacts": rows, "total": total,
                "due_follow_ups": store.count_due_follow_ups(),
                "due_tasks": store.count_due_tasks(),
                "due_tasks_mine": store.count_due_tasks(assignee=agent["agent_id"])}

    @app.post("/api/workspace/contact/{contact_id}/follow-up")
    async def api_workspace_follow_up_add(
        contact_id: str, request: Request, _=Depends(api_auth),
    ):
        """为客户新增跟进任务：{due_at, note, assignee?}。"""
        body = await request.json()
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        try:
            due_at = int(body.get("due_at") or 0)
        except (TypeError, ValueError):
            raise HTTPException(400, "due_at 必须是时间戳整数")
        if due_at <= 0:
            raise HTTPException(400, "due_at 不能为空")
        agent = _session_agent(request)
        assignee = str(body.get("assignee") or "").strip() or agent["agent_id"]
        out = gw.add_follow_up_task(
            contact_id, due_at=due_at, note=str(body.get("note") or ""),
            assignee=assignee, operator=agent["agent_id"],
        )
        if out.get("ok"):
            _publish_follow_up("added", contact_id=contact_id,
                               task_id=out.get("task_id") or "", assignee=assignee)
        return out

    @app.post("/api/workspace/follow-up/{task_id}/done")
    async def api_workspace_follow_up_done(
        task_id: str, request: Request, _=Depends(api_auth),
    ):
        """标记跟进任务完成。"""
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        agent = _session_agent(request)
        out = gw.complete_follow_up_task(task_id, operator=agent["agent_id"])
        if out.get("ok"):
            _publish_follow_up("done", task_id=task_id)
        return out

    @app.post("/api/workspace/follow-up/{task_id}/assign")
    async def api_workspace_follow_up_assign(
        task_id: str, request: Request, _=Depends(api_auth),
    ):
        """改派跟进任务给某坐席：{assignee}。"""
        body = await request.json()
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        assignee = str(body.get("assignee") or "").strip()
        if not assignee:
            raise HTTPException(400, "assignee 不能为空")
        agent = _session_agent(request)
        out = gw.reassign_follow_up_task(
            task_id, assignee=assignee, operator=agent["agent_id"])
        if out.get("ok"):
            _publish_follow_up("assigned", contact_id=out.get("contact_id") or "",
                               task_id=task_id, assignee=assignee)
        return out

    @app.post("/api/workspace/follow-up/{task_id}/snooze")
    async def api_workspace_follow_up_snooze(
        task_id: str, request: Request, _=Depends(api_auth),
    ):
        """延期跟进任务：{days} 顺延 或 {due_at} 直设。"""
        body = await request.json()
        gw = _contacts_gateway(request)
        if gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        try:
            days = int(body.get("days") or 0)
            due_at = int(body.get("due_at") or 0)
        except (TypeError, ValueError):
            raise HTTPException(400, "days/due_at 必须是整数")
        if days <= 0 and due_at <= 0:
            raise HTTPException(400, "需提供 days 或 due_at")
        agent = _session_agent(request)
        out = gw.snooze_follow_up_task(
            task_id, days=days, due_at=due_at, operator=agent["agent_id"])
        if out.get("ok"):
            _publish_follow_up("snoozed", contact_id=out.get("contact_id") or "",
                               task_id=task_id)
        return out

    @app.get("/api/workspace/my-tasks")
    async def api_workspace_my_tasks(
        request: Request, scope: str = "mine", due: str = "today", limit: int = 100,
    ):
        """跟进待办列表：scope=mine(本人)/all(全部)，due=today/overdue/all。"""
        api_auth(request)
        store = _contacts_store(request)
        if store is None:
            return {"ok": False, "error": "contacts_disabled", "tasks": []}
        agent = _session_agent(request)
        assignee = agent["agent_id"] if scope != "all" else None
        now = int(time.time())
        if due == "overdue":
            due_before: Optional[int] = now
        elif due == "all":
            due_before = None
        else:  # today（含逾期 + 今天到期）
            lt = time.localtime(now)
            due_before = int(time.mktime(
                (lt.tm_year, lt.tm_mon, lt.tm_mday, 23, 59, 59, 0, 0, -1)))
        tasks = store.list_open_tasks(
            assignee=assignee, due_before=due_before,
            limit=max(1, min(500, int(limit or 100))))
        for t in tasks:
            t["channel_labels"] = [_PLATFORM_LABELS.get(c, c) for c in (t.get("channels") or [])]
            t["overdue"] = bool(t.get("due_at") and t["due_at"] <= now)
        return {"ok": True, "tasks": tasks,
                "due_tasks": store.count_due_tasks(),
                "due_tasks_mine": store.count_due_tasks(assignee=agent["agent_id"])}

    @app.get("/api/workspace/contact/{contact_id}/tasks")
    async def api_workspace_contact_tasks(
        contact_id: str, request: Request, include_done: int = 0,
    ):
        """某客户的跟进任务（会话内联面板用，轻量）。"""
        api_auth(request)
        store = _contacts_store(request)
        if store is None:
            return {"ok": False, "tasks": []}
        return {"ok": True,
                "tasks": store.list_follow_up_tasks(
                    contact_id, include_done=bool(include_done))}

    # 巨石拆分 slice 17：CRM 客户列表 CSV 导出（slice 12 遗留的 contacts 域 straggler 归并入位）。
    @app.get("/api/workspace/contacts/export.csv")
    async def api_workspace_contacts_export(
        request: Request,
        q: str = "",
        stage: str = "",
        has_lead: str = "",
        tag: str = "",
        follow_up: str = "",
        limit: int = 5000,
    ):
        """按当前筛选导出客户列表 CSV（最多 limit 行）。"""
        api_auth(request)
        store = _contacts_store(request)
        if store is None:
            raise HTTPException(503, "contacts 未启用")
        lead_filter: Optional[bool] = None
        if has_lead in ("1", "true", "yes"):
            lead_filter = True
        elif has_lead in ("0", "false", "no"):
            lead_filter = False
        fu = follow_up if follow_up in ("due", "any") else ""
        rows, _total = store.list_contacts_overview(
            q=str(q or "").strip(), stage=str(stage or "").strip(),
            has_lead=lead_filter, tag=str(tag or "").strip(), follow_up=fu,
            limit=max(1, min(20000, int(limit or 5000))), offset=0,
        )
        import csv
        import io
        buf = io.StringIO()
        buf.write("\ufeff")  # Excel UTF-8 BOM
        w = csv.writer(buf)
        w.writerow(["contact_id", "name", "channels", "funnel_stage",
                    "intimacy", "has_lead", "tags", "follow_up_at", "last_active_at"])
        for r in rows:
            stage_lbl = FUNNEL_STAGE_LABELS.get(r.get("funnel_stage") or "",
                                                r.get("funnel_stage") or "")
            w.writerow([
                r.get("contact_id") or "",
                r.get("primary_name") or "",
                " ".join(_PLATFORM_LABELS.get(c, c) for c in (r.get("channels") or [])),
                stage_lbl,
                "" if r.get("intimacy_score") is None else r.get("intimacy_score"),
                "1" if r.get("has_lead") else "0",
                " ".join(r.get("tags") or []),
                _fmt_ts(r.get("follow_up_at") or 0),
                _fmt_ts(r.get("last_active_at") or 0),
            ])
        return Response(
            content=buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=contacts.csv"},
        )
