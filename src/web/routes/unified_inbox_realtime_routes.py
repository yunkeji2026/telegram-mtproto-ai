"""统一收件箱——协作实时推送路由域（巨石拆分 slice 36）。

把 ``register_unified_inbox_routes`` 巨型闭包中连续的 SSE + typing 子域整体外移为
``register_realtime_routes(app, *, api_auth)``，由主 register 在**原位置**调用：

- ``workspace/stream``：SSE 实时推送（inbox 事件 + SLA/升级边沿告警 + 通知队列）
- ``workspace/typing``：多坐席打字状态协同

端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫 + slice 36 端点契约断言）。

依赖全部朝下：auth.(_SUPERVISOR_ROLES/_is_supervisor/_session_agent)、
services._inbox_store、sla.(_sla_alert_snapshot/_escalation_snapshot/_presence_stale_sec)、
event_bus（handler 内局部 import）。只收 api_auth 一个参数（stream 内联 api_auth 调用）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List

from fastapi import Depends, HTTPException, Request
from starlette.responses import StreamingResponse

from src.integrations.shared.event_bus import get_event_bus
from src.web.routes.unified_inbox_auth import (
    _SUPERVISOR_ROLES,
    _is_supervisor,
    _session_agent,
)
from src.web.routes.unified_inbox_services import _inbox_store
from src.web.routes.unified_inbox_sla import (
    _escalation_snapshot,
    _presence_stale_sec,
    _sla_alert_snapshot,
)

logger = logging.getLogger(__name__)

# SSE replay / live 订阅事件类型（与 monolith 原集合一致）
_SSE_EVENT_TYPES = frozenset({
    "inbox_message", "agent_presence",
    "conversation_claim", "follow_up",
    "draft_created",
    "draft_sla_breach",
    "draft_reassigned",
    "typing",
    "anomaly_alert",
    "sla_alert",
    "conv_note",
    "queue_alert",
    "stage_advance",
    "stage_advance_pending",
    "ops_report",
    "stage_downgrade",
    "stage_reunion",
    "stage_sync",
    "workflow_step",
    "workflow_execution_completed",
    "workflow_execution_failed",
    "workflow_execution_cancelled",
    "health_alert",
    "billing_alert",
    "ops_report",
})

# 写入 app.state.notif_queue 的重要事件类型
_NOTIF_EVENT_TYPES = frozenset({
    "inbox_message", "draft_sla_breach", "draft_reassigned",
    "anomaly_alert", "sla_alert", "escalation", "queue_alert",
    "stage_advance", "stage_advance_pending", "stage_downgrade",
    "stage_reunion", "stage_sync", "workflow_step",
    "workflow_execution_completed", "workflow_execution_failed",
    "workflow_execution_cancelled",
    "health_alert",
    "billing_alert",
    "ops_report",
})


def register_realtime_routes(app, *, api_auth) -> None:
    """挂载 SSE 实时推送 + typing 协同端点。"""

    @app.get("/api/workspace/stream")
    async def api_workspace_stream(request: Request):
        """SSE：实时推送收件箱新消息事件（替代前端轮询）。"""
        api_auth(request)
        import json as _json

        bus = get_event_bus()
        queue = bus.subscribe()

        _sla_seen: set = set()

        def _sla_pushes():
            """边沿触发：返回本轮"新转入严重超时"的会话 SSE 帧（去重 + 恢复后可再报）。"""
            frames: List[str] = []
            try:
                snap = _sla_alert_snapshot(request)
                items = snap.get("items", [])
                cur = {it["conversation_id"] for it in items}
                for it in items:
                    cid = it["conversation_id"]
                    if cid not in _sla_seen:
                        _sla_seen.add(cid)
                        frames.append(
                            "data: " + _json.dumps(
                                {"type": "sla_alert", "data": it},
                                ensure_ascii=False) + "\n\n")
                _sla_seen.intersection_update(cur)
            except Exception:
                logger.debug("SLA SSE 推送计算失败（已忽略）", exc_info=True)
            return frames

        _esc_seen: set = set()

        def _pick_assigned_supervisor(inbox) -> str:
            """负载均衡：从在线主管中选当前指派数最少的那个。
            都不在线或无法确定时返回空串（保留广播语义）。"""
            if inbox is None:
                return ""
            try:
                now = time.time()
                since = now - 86400  # 24 h 窗口内的已指派数
                presence = inbox.list_agent_presence(
                    active_within_sec=_presence_stale_sec(request))
                online = [p for p in presence
                          if p.get("status") in ("online", "busy")]
                if not online:
                    return ""
                sups = [p for p in online
                        if str(p.get("role") or "") in _SUPERVISOR_ROLES]
                pool = sups if sups else online
                best = min(
                    pool,
                    key=lambda p: inbox.count_assigned_escalations(
                        str(p["agent_id"]), since_ts=since),
                )
                return str(best.get("agent_id") or "")
            except Exception:
                logger.debug("auto-assign supervisor 失败（已忽略）", exc_info=True)
                return ""

        def _esc_pushes():
            """边沿触发：新升级 → 审计落库 + 自动指派主管 + 推定向 SSE 帧。"""
            frames: List[str] = []
            try:
                snap = _escalation_snapshot(request)
                items = snap.get("items", [])
                cur = {it["conversation_id"] for it in items}
                inbox = _inbox_store(request)
                for it in items:
                    cid = it["conversation_id"]
                    if cid in _esc_seen:
                        continue
                    _esc_seen.add(cid)
                    assigned_to = ""
                    if inbox is not None:
                        try:
                            is_new = inbox.record_escalation(
                                cid, reason=it.get("reason", ""),
                                agent_id=it.get("agent_id", ""),
                                agent_name=it.get("agent_name", ""),
                                wait_sec=it.get("wait_sec", 0))
                            if is_new:
                                assigned_to = _pick_assigned_supervisor(inbox)
                                if assigned_to:
                                    try:
                                        rows = inbox.list_escalations(
                                            since_ts=time.time() - 10, limit=5)
                                        esc_id = next(
                                            (r["id"] for r in rows
                                             if r.get("conversation_id") == cid),
                                            None)
                                        if esc_id is not None:
                                            inbox.set_escalation_assigned(
                                                esc_id, assigned_to)
                                    except Exception:
                                        logger.debug("set_escalation_assigned 失败",
                                                     exc_info=True)
                            else:
                                try:
                                    rows = inbox.list_escalations(
                                        since_ts=time.time() - 3600, limit=20)
                                    existing = next(
                                        (r for r in rows
                                         if r.get("conversation_id") == cid), None)
                                    assigned_to = str(
                                        (existing or {}).get("assigned_to") or "")
                                except Exception:
                                    pass
                        except Exception:
                            logger.debug("升级审计落库失败（已忽略）", exc_info=True)
                    payload = dict(it)
                    payload["assigned_to"] = assigned_to
                    frames.append(
                        "data: " + _json.dumps(
                            {"type": "escalation", "data": payload},
                            ensure_ascii=False) + "\n\n")
                _esc_seen.intersection_update(cur)
            except Exception:
                logger.debug("升级 SSE 推送计算失败（已忽略）", exc_info=True)
            return frames

        def _maybe_push_notif(evt: dict):
            """将重要事件写入 app.state.notif_queue（P24 通知中心）。"""
            if evt.get("type") not in _NOTIF_EVENT_TYPES:
                return
            nq: list = getattr(request.app.state, "notif_queue", None)
            if nq is None:
                nq = []
                request.app.state.notif_queue = nq
            nq.append({**evt, "_notif_ts": int(time.time() * 1000)})
            if len(nq) > 200:
                del nq[:-200]

        async def _gen():
            try:
                for evt in bus.recent_events(30):
                    if evt.get("type") in _SSE_EVENT_TYPES:
                        yield f"data: {_json.dumps(evt, ensure_ascii=False)}\n\n"
                        _maybe_push_notif(evt)
                for fr in _sla_pushes():
                    yield fr
                for fr in _esc_pushes():
                    yield fr
                while True:
                    try:
                        evt = await asyncio.wait_for(queue.get(), timeout=30.0)
                        if evt.get("type") in _SSE_EVENT_TYPES:
                            yield f"data: {_json.dumps(evt, ensure_ascii=False)}\n\n"
                            _maybe_push_notif(evt)
                    except asyncio.TimeoutError:
                        yield ": heartbeat\n\n"
                        for fr in _sla_pushes():
                            yield fr
                        for fr in _esc_pushes():
                            yield fr
                        try:
                            _watcher = getattr(request.app.state, "sla_watcher", None)
                            if _watcher is not None and _is_supervisor(request):
                                snap = _watcher.status_snapshot()
                                yield "data: " + _json.dumps({
                                    "type": "sla_watcher_status",
                                    "data": snap,
                                }, ensure_ascii=False) + "\n\n"
                        except Exception:
                            pass
                    if await request.is_disconnected():
                        break
            finally:
                bus.unsubscribe(queue)

        return StreamingResponse(
            _gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/workspace/typing")
    async def api_workspace_typing(request: Request, _=Depends(api_auth)):
        """Phase 11：多坐席打字状态协同 — 向同一对话的其他坐席发送实时 typing 事件。"""
        body = await request.json()
        conversation_id = str(body.get("conversation_id") or "").strip()
        if not conversation_id:
            raise HTTPException(400, "conversation_id 必填")
        agent = _session_agent(request)
        try:
            get_event_bus().publish("typing", {
                "conversation_id": conversation_id,
                "agent_id": agent["agent_id"],
                "agent_name": agent["display_name"],
                "ts": time.time(),
            })
        except Exception:
            logger.debug("typing 事件发布失败", exc_info=True)
        return {"ok": True}
