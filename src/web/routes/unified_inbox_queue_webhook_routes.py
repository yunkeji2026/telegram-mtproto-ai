"""统一收件箱——Queue Monitor 实时看板 / Webhook 外发配置路由域（巨石拆分 slice 23）。

把 ``register_unified_inbox_routes`` 巨型闭包中相邻的运维/可观测·配置两段子域整体外移为
``register_queue_webhook_routes(app, *, api_auth)``，由主 register 在**原位置**调用：

- Phase 29 Queue Monitor 实时看板：``queue-monitor`` + ``queue-monitor/reassign``
- Phase 28 Webhook 外发运行时配置：``webhook-outbound`` + ``webhook-outbound/test``

端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫 + slice 23 端点契约断言）。

依赖全部朝下：services._inbox_store、auth._is_supervisor、sla._presence_stale_sec；
event_bus 为 handler 内局部 import。只收 api_auth 一个参数（零闭包私有依赖）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import Depends, HTTPException, Request

from src.web.routes.unified_inbox_auth import _is_supervisor
from src.web.routes.unified_inbox_services import _inbox_store
from src.web.routes.unified_inbox_sla import _presence_stale_sec
from src.web.web_i18n import tr

logger = logging.getLogger(__name__)


def register_queue_webhook_routes(app, *, api_auth) -> None:
    """挂载 Queue Monitor 看板/重分配 + Webhook 外发配置/测试端点。"""

    # ─── Phase 29: Queue Monitor 实时看板 ──────────────────────────────

    @app.get("/api/workspace/queue-monitor")
    async def api_queue_monitor(request: Request):
        """P29：实时运营看板——每坐席工作量快照 + 全局队列指标。

        返回：
          agents: [{agent_id, agent_name, status, open_convs, unread_total,
                    avg_wait_sec, oldest_wait_sec, load_pct}]
          queue:  {total_open, total_unread, avg_wait_sec, crit_count,
                   unassigned_count}
          ts: float  — 快照时间戳
        """
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": tr(request, "err.svc.inbox_not_ready")}

        import time as _t
        now = _t.time()
        stale_sec = _presence_stale_sec(request)

        # 1. 在线坐席
        presence = store.list_agent_presence(active_within_sec=stale_sec) if store else []
        agent_map: Dict[str, Dict[str, Any]] = {
            p["agent_id"]: {
                "agent_id": p["agent_id"],
                "agent_name": p.get("display_name") or p["agent_id"],
                "status": p.get("status", "offline"),
                "open_convs": 0,
                "unread_total": 0,
                "wait_secs": [],
                "avg_wait_sec": 0,
                "oldest_wait_sec": 0,
                "load_pct": 0,
            }
            for p in presence
        }
        # 未认领的虚拟坐席桶
        agent_map["__unassigned__"] = {
            "agent_id": "__unassigned__",
            "agent_name": "（未认领）",
            "status": "virtual",
            "open_convs": 0, "unread_total": 0,
            "wait_secs": [], "avg_wait_sec": 0,
            "oldest_wait_sec": 0, "load_pct": 0,
        }

        # 2. 遍历全量未归档会话（limit 500）
        convs = store.list_conversations(limit=500)
        total_unread = 0
        crit_count = 0
        unassigned_count = 0
        all_wait_secs: list = []

        for c in convs:
            # 跳过已归档
            meta = store.get_conv_meta(c.get("conversation_id", "")) or {}
            if meta.get("archived"):
                continue

            claimed = str(c.get("claimed_by") or "").strip()
            bucket = claimed if claimed in agent_map else "__unassigned__"
            if not claimed:
                unassigned_count += 1

            agent_map[bucket]["open_convs"] += 1
            unread = int(c.get("unread") or 0)
            agent_map[bucket]["unread_total"] += unread
            total_unread += unread

            wait = int(c.get("unanswered_sec") or 0)
            if wait > 0:
                agent_map[bucket]["wait_secs"].append(wait)
                all_wait_secs.append(wait)
            if c.get("sla_level") == "crit":
                crit_count += 1

        # 3. 计算每坐席统计
        max_open = max((a["open_convs"] for a in agent_map.values()), default=1) or 1
        for a in agent_map.values():
            ws = a.pop("wait_secs")
            a["avg_wait_sec"] = int(sum(ws) / len(ws)) if ws else 0
            a["oldest_wait_sec"] = int(max(ws)) if ws else 0
            a["load_pct"] = round(a["open_convs"] / max_open * 100)

        # 排序：在线 → 忙碌 → 离线；同状态按工作量降序
        _status_order = {"online": 0, "busy": 1, "offline": 2, "virtual": 3}
        agents_list = sorted(
            agent_map.values(),
            key=lambda a: (_status_order.get(a["status"], 9), -a["open_convs"]),
        )

        avg_wait_global = int(sum(all_wait_secs) / len(all_wait_secs)) if all_wait_secs else 0

        return {
            "ok": True,
            "agents": agents_list,
            "queue": {
                "total_open": sum(a["open_convs"] for a in agents_list),
                "total_unread": total_unread,
                "avg_wait_sec": avg_wait_global,
                "crit_count": crit_count,
                "unassigned_count": unassigned_count,
            },
            "ts": now,
        }

    # ─── Phase 28: Webhook 外发运行时配置 ─────────────────────────────

    @app.get("/api/workspace/webhook-outbound")
    async def api_webhook_outbound_list(request: Request, _=Depends(api_auth)):
        """P28：列出当前已配置的出站 Webhook（含事件别名、格式）。"""
        notifier = getattr(request.app.state, "webhook_notifier", None)
        if notifier is None:
            return {"ok": True, "webhooks": [], "note": "WebhookNotifier 未启动"}
        # 脱敏 secret
        hooks = []
        for m in getattr(notifier, "_matchers", []):
            hooks.append({
                "url": m.get("url", ""),
                "name": m.get("name", ""),
                "fmt": m.get("fmt", "json"),
                "types": list(m.get("types") or ["all"]),
                "has_secret": bool(m.get("secret")),
            })
        return {
            "ok": True,
            "webhooks": hooks,
            "total_sent": getattr(notifier, "total_sent", 0),
            "total_errors": getattr(notifier, "total_errors", 0),
        }

    @app.post("/api/workspace/webhook-outbound/test")
    async def api_webhook_outbound_test(request: Request, _=Depends(api_auth)):
        """P28：向所有已配置 Webhook 发送测试事件（ping）。"""
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        import time as _t
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish({
                "type": "report",
                "subtype": "webhook_test",
                "data": {"message": "Webhook 测试 Ping", "ts": _t.time()},
                "ts": _t.time(),
            })
            return {"ok": True, "message": "测试事件已发布到事件总线"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.post("/api/workspace/queue-monitor/reassign")
    async def api_queue_reassign(request: Request, _=Depends(api_auth)):
        """P29：将指定会话重新分配给另一坐席（主管操作）。

        Body: {conversation_id: str, to_agent_id: str}
        """
        if not _is_supervisor(request):
            raise HTTPException(403, tr(request, "err.perm.supervisor_required"))
        body = await request.json()
        cid = str(body.get("conversation_id") or "").strip()
        to_agent = str(body.get("to_agent_id") or "").strip()
        if not cid or not to_agent:
            raise HTTPException(422, tr(request, "err.ws.field_required", field="conversation_id / to_agent_id"))
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, tr(request, "err.svc.inbox_not_ready"))
        store.update_conv_meta(cid, {"claimed_by": to_agent})
        # 事件总线广播（通知目标坐席）
        try:
            from src.integrations.shared.event_bus import get_event_bus
            import time as _t
            get_event_bus().publish({
                "type": "conversation_claim",
                "data": {"conversation_id": cid, "agent_id": to_agent,
                         "action": "reassigned_by_supervisor"},
                "ts": _t.time(),
            })
        except Exception:
            pass
        return {"ok": True, "conversation_id": cid, "to_agent_id": to_agent}
