"""统一收件箱——分流路由规则引擎 / 全局跨资源搜索路由域（巨石拆分 slice 21）。

把两段相邻且共享依赖面的子域，从 ``register_unified_inbox_routes`` 巨型闭包中整体外移为
``register_routing_search_routes(app, *, api_auth)``，由主 register 在**原位置**调用：

- Phase 38 分流路由规则引擎：``routing-rules`` CRUD + ``routing-rules/evaluate``
- Phase 39 全局跨资源搜索：``search``（消息/注解/联系人合并排序）

端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫 + slice 21 端点契约断言）。

依赖全部朝下：services 存储（_inbox_store / _contacts_store）。只收 api_auth 一个参数
（零闭包私有依赖）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import Depends, Request

from src.web.routes.unified_inbox_services import _contacts_store, _inbox_store
from src.web.web_i18n import tr

logger = logging.getLogger(__name__)


def register_routing_search_routes(app, *, api_auth) -> None:
    """挂载分流路由规则 CRUD/评估 + 全局跨资源搜索端点。"""

    # ─── Phase 38: 分流路由规则引擎 ─────────────────────────────────────

    @app.get("/api/workspace/routing-rules")
    async def api_routing_rules_list(request: Request):
        """BB1：列出所有分流路由规则（按优先级降序）。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": True, "rules": []}
        import json as _j
        rules = store.list_routing_rules()
        for r in rules:
            try:
                r["conditions"] = _j.loads(r.get("conditions") or "{}")
            except Exception:
                r["conditions"] = {}
        return {"ok": True, "rules": rules}

    @app.post("/api/workspace/routing-rules")
    async def api_routing_rules_create(request: Request, _=Depends(api_auth)):
        body = await request.json()
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": tr(request, "err.svc.inbox_not_ready")}
        rule_id = store.upsert_routing_rule(body)
        return {"ok": True, "rule_id": rule_id}

    @app.put("/api/workspace/routing-rules/{rule_id}")
    async def api_routing_rules_update(rule_id: str, request: Request, _=Depends(api_auth)):
        body = await request.json()
        body["rule_id"] = rule_id
        store = _inbox_store(request)
        if store is None:
            return {"ok": False}
        store.upsert_routing_rule(body)
        return {"ok": True, "rule_id": rule_id}

    @app.delete("/api/workspace/routing-rules/{rule_id}")
    async def api_routing_rules_delete(rule_id: str, request: Request, _=Depends(api_auth)):
        store = _inbox_store(request)
        if store is None:
            return {"ok": False}
        ok = store.delete_routing_rule(rule_id)
        return {"ok": ok}

    @app.post("/api/workspace/routing-rules/evaluate")
    async def api_routing_rules_evaluate(request: Request, _=Depends(api_auth)):
        """BB1：对给定会话评估所有路由规则，返回命中的规则和分配目标。"""
        body = await request.json()
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "matched": []}
        import json as _j
        rules = store.list_routing_rules()
        conversation = body.get("conversation") or {}
        platform = str(conversation.get("platform") or "").lower()
        text = str(conversation.get("text") or "").lower()

        matched = []
        for rule in rules:
            if not rule.get("enabled"):
                continue
            try:
                conds = _j.loads(rule.get("conditions") or "{}")
            except Exception:
                conds = {}

            hit = False
            if conds.get("platform") and platform:
                if str(conds["platform"]).lower() == platform:
                    hit = True
            if conds.get("keyword") and text:
                if str(conds["keyword"]).lower() in text:
                    hit = True
            if not conds:
                hit = True  # 空条件 = 通配

            if hit:
                matched.append({
                    "rule_id": rule["rule_id"],
                    "name": rule["name"],
                    "assign_to": rule["assign_to"],
                    "priority": rule["priority"],
                })

        # 按优先级排序，取最高优先级命中
        matched.sort(key=lambda x: x["priority"], reverse=True)
        best = matched[0] if matched else None
        return {"ok": True, "matched": matched, "best_match": best}

    # ─── Phase 39: 全局跨资源搜索 ───────────────────────────────────────

    @app.get("/api/workspace/search")
    async def api_workspace_global_search(
        request: Request,
        q: str = "",
        types: str = "messages,contacts,notes",
        limit: int = 20,
    ):
        """CC1：全局搜索（消息/联系人/注解，结果合并按相关度排序）。"""
        api_auth(request)
        q = str(q or "").strip()
        if not q or len(q) < 2:
            return {"ok": True, "q": q, "results": [], "total": 0}
        limit = max(1, min(50, int(limit or 20)))
        search_types = set(str(types or "").split(","))
        store = _inbox_store(request)
        results: List[Dict[str, Any]] = []

        if store is not None:
            # 1. 消息搜索（FTS5 优先）
            if "messages" in search_types:
                try:
                    msg_results = store.search_messages(q, limit=limit)
                    for m in msg_results:
                        results.append({
                            "type": "message",
                            "icon": "💬",
                            "title": str(m.get("display_name") or m.get("conversation_id") or ""),
                            "preview": str(m.get("text") or "")[:100],
                            "ts": m.get("ts"),
                            "conversation_id": m.get("conversation_id"),
                            "platform": m.get("platform", ""),
                            "url": f"/workspace?focus={m.get('conversation_id', '')}",
                        })
                except Exception:
                    logger.debug("global search 消息搜索失败", exc_info=True)

            # 2. 注解搜索
            if "notes" in search_types:
                try:
                    with store._lock:
                        note_rows = store._conn.execute(
                            """SELECT n.note_id, n.conversation_id, n.body, n.ts, n.agent_name,
                                      c.display_name, c.platform
                               FROM conv_notes n
                               LEFT JOIN conversations c ON c.conversation_id = n.conversation_id
                               WHERE n.body LIKE ?
                               ORDER BY n.ts DESC LIMIT ?""",
                            (f"%{q}%", limit),
                        ).fetchall()
                    for r in note_rows:
                        results.append({
                            "type": "note",
                            "icon": "📝",
                            "title": f"注解 · {r['display_name'] or r['conversation_id']}",
                            "preview": str(r["body"] or "")[:100],
                            "ts": r["ts"],
                            "conversation_id": r["conversation_id"],
                            "platform": r.get("platform", ""),
                            "url": f"/workspace?focus={r['conversation_id']}",
                        })
                except Exception:
                    logger.debug("global search 注解搜索失败", exc_info=True)

        # 3. 联系人搜索
        if "contacts" in search_types:
            contacts_store = _contacts_store(request)
            if contacts_store is not None:
                try:
                    contacts, _ = contacts_store.list_contacts_overview(q=q, limit=limit)
                    for c in contacts:
                        results.append({
                            "type": "contact",
                            "icon": "👤",
                            "title": str(c.get("primary_name") or c.get("contact_id") or ""),
                            "preview": " / ".join(c.get("channels") or []),
                            "ts": c.get("last_seen_ts") or c.get("created_at"),
                            "contact_id": c.get("contact_id"),
                            "url": f"/workspace/contact/{c.get('contact_id', '')}",
                        })
                except Exception:
                    logger.debug("global search 联系人搜索失败", exc_info=True)

        # 全局按 ts 降序，截断
        results.sort(key=lambda x: float(x.get("ts") or 0), reverse=True)
        results = results[:limit]
        return {"ok": True, "q": q, "results": results, "total": len(results)}
