"""情景记忆 + 跨平台身份 API 路由（Phase E1 续拆，从 admin.py 抽出）。

两者同域：CrossPlatformIdentity 的 link/unlink 正是为了让多平台 UID 共享同一份
情景记忆。仅迁移 API 端点（页面路由因需 templates 仍留 admin.py，与既有约定一致）。
行为与抽出前一致；依赖经 AdminRouteContext 注入。
"""

from __future__ import annotations

import time
from typing import Any, Dict

from fastapi import HTTPException, Request


def build_correction_stats(
    audit_store: Any,
    skill_manager: Any,
    *,
    days: int = 30,
    recent_limit: int = 10,
    with_trend: bool = True,
) -> Dict[str, Any]:
    """R17/R18：聚合"AI 推断→人工确认"质量指标。

    采纳数来自审计（action=episodic_confirm_inferred）；待确认数来自记忆库当前 raw 的
    ai_inferred。采纳率为近似 confirmed/(confirmed+pending)。供 correction-stats 端点
    与 alert-status 低采纳告警共用，避免聚合逻辑两处漂移。
    """
    win = max(1, min(int(days or 30), 365))
    since = time.strftime(
        "%Y-%m-%d %H:%M:%S", time.localtime(time.time() - win * 86400)
    )
    rows = []
    if audit_store:
        try:
            rows = audit_store.query(
                limit=5000, action="episodic_confirm_inferred", since=since,
            ) or []
        except Exception:
            rows = []
    by_actor: Dict[str, int] = {}
    daily: Dict[str, int] = {}
    recent = []
    for r in rows:
        actor = str(r.get("user_id") or "?")
        by_actor[actor] = by_actor.get(actor, 0) + 1
        day = str(r.get("ts") or "")[:10]
        if day:
            daily[day] = daily.get(day, 0) + 1
        if len(recent) < max(0, int(recent_limit)):
            recent.append({
                "ts": r.get("ts", ""),
                "actor": actor,
                "content": str(r.get("new_val") or ""),
                "target": str(r.get("target") or ""),
            })
    confirmed = len(rows)
    inferred = {"pending": 0, "total": 0}
    if skill_manager and hasattr(skill_manager, "episodic_inferred_counts"):
        try:
            inferred = skill_manager.episodic_inferred_counts()
        except Exception:
            inferred = {"pending": 0, "total": 0}
    pending = int(inferred.get("pending", 0) or 0)
    denom = confirmed + pending
    adoption_rate = round(confirmed / denom, 4) if denom else 0.0
    out: Dict[str, Any] = {
        "ok": True,
        "window_days": win,
        "confirmed": confirmed,
        "pending_inferred": pending,
        "total_inferred": int(inferred.get("total", 0) or 0),
        "adoption_rate": adoption_rate,
        "sample": denom,
        "by_actor": sorted(
            [{"actor": a, "count": c} for a, c in by_actor.items()],
            key=lambda x: x["count"], reverse=True,
        ),
        "recent": recent,
    }
    if with_trend:
        out["trend"] = [
            {"date": d, "count": daily[d]} for d in sorted(daily)
        ]
    return out


def register_episodic_identity_routes(app, ctx) -> None:
    """挂载 /api/episodic-memory/* 与 /api/identity/* 到 app。"""
    telegram_client = ctx.telegram_client
    _api_auth = ctx.api_auth
    _api_write = ctx.api_write

    # ── 情景记忆 API ──────────────────────────────────────────────────────

    @app.get("/api/episodic-memory")
    async def api_episodic_memory_list(
        request: Request, prefix: str = "", limit: int = 100, source: str = "",
    ):
        """情景记忆条目列表（memory_key = 私聊用户 id 或 群id_用户id）。

        R13：可选 ``source`` 筛选（user_stated / ai_inferred）。
        """
        _api_auth(request)
        if not telegram_client or not getattr(telegram_client, "skill_manager", None):
            raise HTTPException(status_code=503, detail="Bot 未就绪或未注入 SkillManager")
        sm = telegram_client.skill_manager
        lim = max(1, min(int(limit or 100), 500))
        src = source if source in ("user_stated", "ai_inferred") else ""
        rows = sm.episodic_list_for_admin(prefix=prefix[:120], limit=lim, source=src)
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

    @app.get("/api/episodic-memory/correction-stats")
    async def api_episodic_correction_stats(request: Request, days: int = 30):
        """R17：记忆校正质量看板——AI 推断采纳量/采纳率 + 各坐席确认量。

        采纳数来自审计（action=episodic_confirm_inferred）；待确认数来自记忆库当前
        raw 的 ai_inferred。采纳率为近似：confirmed/(confirmed+pending)。
        """
        _api_auth(request)
        sm = getattr(telegram_client, "skill_manager", None) if telegram_client else None
        return build_correction_stats(
            getattr(ctx, "audit_store", None), sm, days=int(days or 30),
        )

    @app.post("/api/episodic-memory/{row_id}/confirm")
    async def api_episodic_memory_confirm(request: Request, row_id: int):
        """R15/R16：确认一条 AI 推断为属实——升格 user_stated 且置 stable，并落审计。"""
        _api_write("episodic_memory")(request)
        if not telegram_client or not getattr(telegram_client, "skill_manager", None):
            raise HTTPException(status_code=503, detail="Bot 未就绪")
        content = telegram_client.skill_manager.episodic_confirm_for_admin(int(row_id))
        if not content:
            raise HTTPException(status_code=404, detail="记录不存在、非 AI 推断或记忆未启用")
        # R16：谁在何时把哪条 AI 推断确认成事实——与危机处置审计对称，便于回溯校正质量
        audit = getattr(ctx, "audit_store", None)
        if audit:
            try:
                actor = str(
                    request.session.get("username")
                    or request.session.get("role")
                    or "web_admin"
                )
                audit.log(
                    actor, "episodic_confirm_inferred",
                    target=str(row_id),
                    old_val="ai_inferred",
                    new_val=str(content)[:200],
                )
            except Exception:
                pass
        return {"ok": True, "confirmed": int(row_id)}

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
