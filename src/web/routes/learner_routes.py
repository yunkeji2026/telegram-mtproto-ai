"""每日自动学习路由 — /api/learner/*（Phase E1 批 5J）。

从 admin.py 抽出，复用 AdminRouteContext。_get_learner 缓存在 app.state._daily_learner，
与 ai-studio summary 等其它读取点共享同一实例。

端点（与抽出前逐行一致）：
  GET  /api/learner/stats           POST /api/learner/run
  GET  /api/learner/drafts          GET  /api/learner/drafts/{draft_id}
  PUT  /api/learner/drafts/{draft_id}
  POST /api/learner/drafts/{draft_id}/approve   POST /api/learner/drafts/{draft_id}/reject
  POST /api/learner/drafts/approve-all          POST /api/learner/drafts/batch-action
  POST /api/learner/drafts/{draft_id}/recheck-dup
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from fastapi import Depends, HTTPException, Query, Request

from src.utils.daily_learner import DailyLearner
from src.utils.domain_policy import effective_domain_name


def register_learner_routes(app, ctx):
    config_manager = ctx.config_manager
    _kb_store = ctx.kb_store
    telegram_client = ctx.telegram_client
    audit_store = ctx.audit_store
    _api_auth = ctx.api_auth
    _kb_db_path = Path(config_manager.config_path).parent / "knowledge_base.db"

    def _get_learner() -> Optional[DailyLearner]:
        if hasattr(app.state, "_daily_learner"):
            return app.state._daily_learner
        ai = getattr(telegram_client, "ai_client", None) if telegram_client else None
        if not ai:
            return None
        learner = DailyLearner(_kb_store, ai, db_path=_kb_db_path)
        app.state._daily_learner = learner
        return learner

    @app.get("/api/learner/stats")
    async def api_learner_stats(request: Request, _=Depends(_api_auth)):
        learner = _get_learner()
        if not learner:
            return {"error": "AI client not available"}
        return learner.stats()

    @app.post("/api/learner/run")
    async def api_learner_run(request: Request, _=Depends(_api_auth)):
        """手动触发一次学习"""
        learner = _get_learner()
        if not learner:
            raise HTTPException(503, "AI client not available")
        domain_ctx = ""
        cfg_obj = config_manager.config if hasattr(config_manager, 'config') else {}
        domain_name = effective_domain_name(cfg_obj if isinstance(cfg_obj, dict) else {})
        if domain_name:
            domain_ctx = f"当前行业: {domain_name}"
        result = await learner.run_daily_learn(domain_context=domain_ctx)
        actor = request.session.get("username", "system")
        if audit_store:
            audit_store.log(actor, "learner_run", json.dumps(result))
        return result

    @app.get("/api/learner/drafts")
    async def api_learner_drafts(request: Request, _=Depends(_api_auth),
                                 status: str = Query("pending"),
                                 sort: str = Query("priority")):
        learner = _get_learner()
        if not learner:
            return {"drafts": []}
        return {"drafts": learner.list_drafts(status=status, sort=sort)}

    @app.get("/api/learner/drafts/{draft_id}")
    async def api_learner_draft_detail(request: Request, draft_id: str,
                                       _=Depends(_api_auth)):
        learner = _get_learner()
        if not learner:
            raise HTTPException(503, "learner not available")
        draft = learner.get_draft(draft_id)
        if not draft:
            raise HTTPException(404, "draft not found")
        return draft

    @app.put("/api/learner/drafts/{draft_id}")
    async def api_learner_draft_update(request: Request, draft_id: str,
                                        _=Depends(_api_auth)):
        learner = _get_learner()
        if not learner:
            raise HTTPException(503, "learner not available")
        body = await request.json()
        learner.update_draft(draft_id, body)
        return {"ok": True}

    @app.post("/api/learner/drafts/{draft_id}/approve")
    async def api_learner_draft_approve(request: Request, draft_id: str,
                                         _=Depends(_api_auth)):
        learner = _get_learner()
        if not learner:
            raise HTTPException(503, "learner not available")
        actor = request.session.get("username", "web_admin")
        entry_id = learner.approve_draft(draft_id, operator=actor)
        if not entry_id:
            raise HTTPException(400, "draft cannot be approved")
        if audit_store:
            audit_store.log(actor, "learner_approve", f"{draft_id} -> {entry_id}")
        return {"ok": True, "entry_id": entry_id}

    @app.post("/api/learner/drafts/{draft_id}/reject")
    async def api_learner_draft_reject(request: Request, draft_id: str,
                                        _=Depends(_api_auth)):
        learner = _get_learner()
        if not learner:
            raise HTTPException(503, "learner not available")
        actor = request.session.get("username", "web_admin")
        learner.reject_draft(draft_id, operator=actor)
        if audit_store:
            audit_store.log(actor, "learner_reject", draft_id)
        return {"ok": True}

    @app.post("/api/learner/drafts/approve-all")
    async def api_learner_approve_all(request: Request, _=Depends(_api_auth)):
        learner = _get_learner()
        if not learner:
            raise HTTPException(503, "learner not available")
        actor = request.session.get("username", "web_admin")
        count = learner.approve_all_pending(operator=actor)
        if audit_store:
            audit_store.log(actor, "learner_approve_all", str(count))
        return {"ok": True, "approved": count}

    @app.post("/api/learner/drafts/batch-action")
    async def api_learner_batch_action(request: Request, _=Depends(_api_auth)):
        """A2: Batch approve/reject selected drafts."""
        learner = _get_learner()
        if not learner:
            raise HTTPException(503, "learner not available")
        body = await request.json()
        ids = body.get("ids", [])
        action = body.get("action", "")
        if not ids or action not in ("approve", "reject"):
            raise HTTPException(400, "ids[] and action (approve|reject) required")
        actor = request.session.get("username", "web_admin")
        result = learner.batch_action(ids, action, operator=actor)
        if audit_store:
            audit_store.log(actor, f"learner_batch_{action}",
                            f"{len(ids)} ids -> {result}")
        return {"ok": True, **result}

    # ── A3: Duplicate recheck ─────────────────────────────────
    @app.post("/api/learner/drafts/{draft_id}/recheck-dup")
    async def api_learner_recheck_dup(request: Request, draft_id: str,
                                       _=Depends(_api_auth)):
        learner = _get_learner()
        if not learner:
            raise HTTPException(503, "learner not available")
        dup = learner.recheck_duplicate(draft_id)
        return {"ok": True, "dup": dup}
