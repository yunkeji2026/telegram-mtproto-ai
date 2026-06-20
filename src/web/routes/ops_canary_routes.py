"""G3 金丝雀放量管理 API（反封号护栏三件套）。

暴露 ``src/ops/canary.py`` 的 cohort 给运营：查看当前可放行成员、手动扩面/移除。
读用 api_auth，写用 manage_ops 权限（与 Kill-Switch 同级）。

端点：
- GET    /api/ops/canary    当前模式 + cohort（pinned ∪ 自动扩面集）
- POST   /api/ops/canary    手动扩面 {members:[...]}（写入持久扩面集，立即放行）
- DELETE /api/ops/canary    移除/清空 {member?}（member 缺省=clear 全部自动扩面集）
"""

from __future__ import annotations

from fastapi import Request


def register_ops_canary_routes(app, ctx) -> None:
    """挂载 /api/ops/canary* 到 app。"""
    _api_auth = ctx.api_auth
    _api_write = ctx.api_write

    def _cfg():
        try:
            cm = getattr(ctx, "config_manager", None)
            if cm is not None and hasattr(cm, "config"):
                return cm.config or {}
            return cm or {}
        except Exception:
            return {}

    def _store():
        from src.ops.canary import get_canary_store
        return get_canary_store()

    @app.get("/api/ops/canary")
    async def api_ops_canary_status(request: Request):
        """当前金丝雀模式与 cohort（启用时 cohort 外账号自动 hold）。"""
        _api_auth(request)
        from src.ops.canary import active_cohort, canary_enabled
        cfg = _cfg()
        canary_cfg = ((cfg.get("ops") or {}).get("canary") or {}) if isinstance(cfg, dict) else {}
        try:
            cohort = sorted(active_cohort(cfg))
        except Exception:
            cohort = []
        return {
            "ok": True,
            "enabled": canary_enabled(cfg),
            "mode": str(canary_cfg.get("mode") or "manual"),
            "cohort": cohort,
            "count": len(cohort),
        }

    @app.post("/api/ops/canary")
    async def api_ops_canary_add(request: Request):
        """手动扩面：把成员写入持久扩面集（立即可放行）。需 manage_ops。

        body：``{members: ["telegram:123", ...]}``（裸 id 视为 telegram）。
        """
        _api_write("manage_ops")(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        from src.ops.canary import member_key
        raw = (body or {}).get("members") or []
        members = set()
        for m in raw:
            s = str(m or "").strip()
            if not s:
                continue
            members.add(s.lower() if ":" in s else member_key("telegram", s))
        if members:
            _store().add(members)
        return {"ok": True, "added": sorted(members)}

    @app.delete("/api/ops/canary")
    async def api_ops_canary_remove(request: Request):
        """移除单个成员或清空全部自动扩面集。需 manage_ops。body：``{member?}``。"""
        _api_write("manage_ops")(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        member = str((body or {}).get("member") or "").strip().lower()
        st = _store()
        if member:
            st.remove(member)
            return {"ok": True, "removed": member}
        st.clear()
        return {"ok": True, "cleared": True}
