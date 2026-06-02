"""运营 case 管理 API 路由（Phase E1 续拆，从 admin.py 抽出）。

端点（与抽出前逐行一致）：
  GET  /api/cases/active            活跃 case 列表（带 _case_id 的上下文）
  POST /api/cases/{case_id}/note    为 case 加备注
  POST /api/cases/{case_id}/close   结案

依赖经 AdminRouteContext 注入（telegram_client/api_auth/audit_store）。
copilot/chat-test 仍留 admin.py（体量大、依赖重），其 _copilot_get_ctx_store 不受影响。
"""

from __future__ import annotations

import time

from fastapi import HTTPException, Request


def register_cases_routes(app, ctx) -> None:
    telegram_client = ctx.telegram_client
    _api_auth = ctx.api_auth
    audit_store = ctx.audit_store

    def _get_ctx_store():
        """获取 context_store 实例（仅依赖 telegram_client）。"""
        if telegram_client:
            sm = getattr(telegram_client, "skill_manager", None)
            if sm:
                return getattr(sm, "_context_store", None)
        return None

    @app.get("/api/cases/active")
    async def api_cases_active(request: Request):
        """返回所有有 _case_id 的活跃用户 case"""
        _api_auth(request)
        ctx_store = _get_ctx_store()
        if not ctx_store:
            return {"cases": [], "count": 0}

        cases = []
        for uid, c in ctx_store._cache.items():
            case_id = c.get("_case_id")
            if not case_id:
                continue
            chain = c.get("_intent_chain", [])
            pattern = c.get("_chain_pattern", {})
            profile = c.get("_user_profile", {})
            cases.append({
                "case_id": case_id,
                "user_id": uid,
                "chat_id": c.get("chat_id", ""),
                "chat_title": c.get("chat_title", ""),
                "intent_chain": chain[-8:],
                "pattern": pattern.get("pattern", "") if isinstance(pattern, dict) else "",
                "pattern_desc": pattern.get("desc", "") if isinstance(pattern, dict) else "",
                "satisfaction": profile.get("satisfaction", 80) if isinstance(profile, dict) else 80,
                "at_risk": profile.get("at_risk", False) if isinstance(profile, dict) else False,
                "consecutive_same": c.get("_consecutive_same_intent", 0),
                "last_message": (c.get("last_message") or "")[:100],
                "last_reply": (c.get("last_reply") or "")[:100],
                "last_active": c.get("last_reply_time", 0),
                "escalation": bool(c.get("_escalation_ts")),
                "closed": c.get("_case_closed", False),
                "note": c.get("_case_note", ""),
            })
        cases.sort(key=lambda x: (x["closed"], not x["at_risk"], -x["consecutive_same"]))
        return {"cases": cases[:100], "count": len(cases)}

    @app.post("/api/cases/{case_id}/note")
    async def api_case_note(request: Request, case_id: str):
        """运营人员为 case 添加备注"""
        _api_auth(request)
        data = await request.json()
        note = (data.get("note") or "").strip()
        ctx_store = _get_ctx_store()
        if not ctx_store:
            raise HTTPException(404, "上下文存储不可用")
        for uid, c in ctx_store._cache.items():
            if c.get("_case_id") == case_id:
                c["_case_note"] = note[:500]
                actor = request.session.get("username", "web_admin")
                if audit_store:
                    audit_store.log(actor, "case_note", case_id, uid, note[:80])
                return {"ok": True, "case_id": case_id}
        raise HTTPException(404, f"Case {case_id} 不存在")

    @app.post("/api/cases/{case_id}/close")
    async def api_case_close(request: Request, case_id: str):
        """运营人员结案"""
        _api_auth(request)
        data = await request.json()
        resolution = (data.get("resolution") or "").strip()
        ctx_store = _get_ctx_store()
        if not ctx_store:
            raise HTTPException(404, "上下文存储不可用")
        for uid, c in ctx_store._cache.items():
            if c.get("_case_id") == case_id:
                c["_case_closed"] = True
                c["_case_resolution"] = resolution[:500]
                c["_case_closed_at"] = time.time()
                actor = request.session.get("username", "web_admin")
                if audit_store:
                    audit_store.log(actor, "case_close", case_id, uid, resolution[:80])
                return {"ok": True, "case_id": case_id}
        raise HTTPException(404, f"Case {case_id} 不存在")
