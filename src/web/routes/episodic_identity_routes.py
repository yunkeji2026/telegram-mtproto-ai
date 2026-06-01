"""情景记忆 + 跨平台身份 API 路由（Phase E1 续拆，从 admin.py 抽出）。

两者同域：CrossPlatformIdentity 的 link/unlink 正是为了让多平台 UID 共享同一份
情景记忆。仅迁移 API 端点（页面路由因需 templates 仍留 admin.py，与既有约定一致）。
行为与抽出前一致；依赖经 AdminRouteContext 注入。
"""

from __future__ import annotations

from fastapi import HTTPException, Request


def register_episodic_identity_routes(app, ctx) -> None:
    """挂载 /api/episodic-memory/* 与 /api/identity/* 到 app。"""
    telegram_client = ctx.telegram_client
    _api_auth = ctx.api_auth
    _api_write = ctx.api_write

    # ── 情景记忆 API ──────────────────────────────────────────────────────

    @app.get("/api/episodic-memory")
    async def api_episodic_memory_list(request: Request, prefix: str = "", limit: int = 100):
        """情景记忆条目列表（memory_key = 私聊用户 id 或 群id_用户id）。"""
        _api_auth(request)
        if not telegram_client or not getattr(telegram_client, "skill_manager", None):
            raise HTTPException(status_code=503, detail="Bot 未就绪或未注入 SkillManager")
        sm = telegram_client.skill_manager
        lim = max(1, min(int(limit or 100), 500))
        rows = sm.episodic_list_for_admin(prefix=prefix[:120], limit=lim)
        return {"ok": True, "items": rows, "count": len(rows)}

    @app.delete("/api/episodic-memory/{row_id}")
    async def api_episodic_memory_delete(request: Request, row_id: int):
        _api_write("episodic_memory")(request)
        if not telegram_client or not getattr(telegram_client, "skill_manager", None):
            raise HTTPException(status_code=503, detail="Bot 未就绪")
        ok = telegram_client.skill_manager.episodic_delete_for_admin(int(row_id))
        if not ok:
            raise HTTPException(status_code=404, detail="记录不存在或记忆未启用")
        return {"ok": True, "deleted": int(row_id)}

    @app.post("/api/episodic-memory/backfill")
    async def api_episodic_memory_backfill(
        request: Request, limit: int = 20, prefix: str = ""
    ):
        """为缺失向量的情景记忆行补全 embedding（限流：单次最多 100 条；可选 prefix 筛选 memory_key）。"""
        _api_write("episodic_memory")(request)
        if not telegram_client or not getattr(telegram_client, "skill_manager", None):
            raise HTTPException(status_code=503, detail="Bot 未就绪")
        sm = telegram_client.skill_manager
        lim = max(1, min(int(limit or 20), 100))
        pre = (prefix or "")[:120]
        out = await sm.episodic_backfill_embeddings(lim, memory_key_prefix=pre)
        if out.get("ok") is False:
            err = str(out.get("error") or "")
            if err == "vector_disabled":
                raise HTTPException(
                    status_code=400, detail="情景记忆向量功能未启用（memory.vector.enabled）"
                )
            if err == "daily_embed_budget_exceeded":
                raise HTTPException(
                    status_code=429,
                    detail="本日情景记忆补全嵌入预算已用尽（memory.vector.daily_embed_budget）",
                )
            if err == "no_store":
                raise HTTPException(
                    status_code=503, detail="情景记忆或 AI 客户端不可用"
                )
            raise HTTPException(status_code=400, detail=err or "backfill_failed")
        return out

    # ── S5: CrossPlatformIdentity API ─────────────────────────────────────

    def _get_cpi():
        """Return CPI instance from SkillManager or None."""
        sm = getattr(telegram_client, "skill_manager", None) if telegram_client else None
        return getattr(sm, "_cpi", None) if sm else None

    @app.get("/api/identity")
    async def api_identity_list(request: Request, limit: int = 200):
        """List all (platform, platform_uid, canonical_id) rows."""
        _api_auth(request)
        cpi = _get_cpi()
        if not cpi:
            raise HTTPException(status_code=503, detail="CrossPlatformIdentity 未就绪")
        rows = cpi.list_all(limit=min(int(limit), 500))
        return {"ok": True, "items": [
            {"platform": r[0], "platform_uid": r[1], "canonical_id": r[2], "created_at": r[3]}
            for r in rows
        ]}

    @app.post("/api/identity/link")
    async def api_identity_link(request: Request):
        """Link two platform UIDs to share the same episodic memory.
        Body: {platform_a, uid_a, platform_b, uid_b}"""
        _api_write("identity")(request)
        cpi = _get_cpi()
        if not cpi:
            raise HTTPException(status_code=503, detail="CrossPlatformIdentity 未就绪")
        body = await request.json()
        pa, ua = str(body.get("platform_a", "")), str(body.get("uid_a", ""))
        pb, ub = str(body.get("platform_b", "")), str(body.get("uid_b", ""))
        if not all([pa, ua, pb, ub]):
            raise HTTPException(status_code=400, detail="需要 platform_a/uid_a/platform_b/uid_b")
        canon = cpi.link(pa, ua, pb, ub)
        return {"ok": True, "canonical_id": canon}

    @app.post("/api/identity/unlink")
    async def api_identity_unlink(request: Request):
        """Detach a platform UID back to its own canonical_id.
        Body: {platform, uid}"""
        _api_write("identity")(request)
        cpi = _get_cpi()
        if not cpi:
            raise HTTPException(status_code=503, detail="CrossPlatformIdentity 未就绪")
        body = await request.json()
        plat, uid = str(body.get("platform", "")), str(body.get("uid", ""))
        if not plat or not uid:
            raise HTTPException(status_code=400, detail="需要 platform 和 uid")
        new_canon = cpi.unlink(plat, uid)
        return {"ok": True, "canonical_id": new_canon}
