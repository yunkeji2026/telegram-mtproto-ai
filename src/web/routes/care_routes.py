"""Phase O4：主动关怀待办 Web API。

把 `CareScheduleStore` 暴露给后台：看「待关怀/已发/跳过(含原因)/过期」+ 手动加/取消。
读写都过 `api_auth`（后台管理面）。store 经 app.state 注入，缺则按 config 目录懒建单例。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import Depends, Request

logger = logging.getLogger(__name__)


def register_care_routes(app, *, api_auth, config_manager=None) -> None:
    def _store(request: Request):
        st = getattr(request.app.state, "care_schedule_store", None)
        if st is not None:
            return st
        from src.contacts.care_schedule import get_care_schedule_store
        db_path = ":memory:"
        try:
            cm = getattr(request.app.state, "config_manager", None) or config_manager
            base = Path(getattr(cm, "config_path", "") or "").parent
            if str(base):
                db_path = base / "care_schedule.db"
        except Exception:
            db_path = ":memory:"
        st = get_care_schedule_store(db_path)
        request.app.state.care_schedule_store = st
        return st

    def _summary(store) -> dict:
        return {s: store.count(status=s)
                for s in ("pending", "sent", "skipped", "expired", "cancelled")}

    @app.get("/api/care/schedule")
    async def api_care_schedule_list(
        request: Request, status: str = "", limit: int = 100, _=Depends(api_auth),
    ):
        """关怀待办列表 + 各状态计数。status 空=全部。"""
        store = _store(request)
        lim = max(1, min(int(limit or 100), 500))
        items = store.list_recent(status=status.strip(), limit=lim)
        return {"ok": True, "items": items, "count": len(items),
                "summary": _summary(store)}

    @app.get("/api/care/schedule/due")
    async def api_care_schedule_due(request: Request, limit: int = 100, _=Depends(api_auth)):
        """当前到期且仍 pending 的待办（预览到点会发什么）。"""
        store = _store(request)
        items = store.list_due(limit=max(1, min(int(limit or 100), 500)))
        return {"ok": True, "items": items, "count": len(items)}

    @app.post("/api/care/schedule")
    async def api_care_schedule_add(request: Request, _=Depends(api_auth)):
        """运营手动加一条关怀（AI 没抽到的约定）。body：
        {contact_key, platform, account_id, chat_key, topic, due_at?|due_in_hours?,
         source_text?, sentiment?}。手动可信 → confidence=1.0，不受阈值/去重拦截。"""
        from src.contacts.care_commitment import CareCommitment

        body = await request.json()
        contact_key = str(body.get("contact_key") or "").strip()
        topic = str(body.get("topic") or "").strip()
        if not contact_key or not topic:
            return {"ok": False, "reason": "missing", "message": "contact_key 和 topic 必填"}
        now = time.time()
        if body.get("due_at"):
            try:
                due_at = float(body["due_at"])
            except Exception:
                return {"ok": False, "reason": "bad_due_at", "message": "due_at 非法"}
        else:
            try:
                due_at = now + float(body.get("due_in_hours", 24)) * 3600.0
            except Exception:
                due_at = now + 86400.0
        if due_at <= now:
            return {"ok": False, "reason": "due_in_past", "message": "到期时间须在未来"}

        commitment = CareCommitment(
            due_at=due_at, event_at=due_at, topic=topic,
            sentiment=str(body.get("sentiment") or "neutral"),
            anchor_text="manual", source_text=str(body.get("source_text") or "")[:160],
            confidence=1.0,
        )
        store = _store(request)
        rid = store.add_commitment(
            commitment, contact_key=contact_key,
            platform=str(body.get("platform") or ""),
            account_id=str(body.get("account_id") or "default"),
            chat_key=str(body.get("chat_key") or ""),
            min_confidence=0.0, dedup_window_days=0.0,
        )
        if not rid:
            return {"ok": False, "reason": "add_failed", "message": "写入失败（可能重复）"}
        return {"ok": True, "id": rid}

    @app.post("/api/care/schedule/{sid}/cancel")
    async def api_care_schedule_cancel(sid: int, request: Request, _=Depends(api_auth)):
        """取消一条 pending 待办。"""
        store = _store(request)
        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        ok = store.cancel(int(sid), note=str(body.get("note") or "")[:200])
        if not ok:
            return {"ok": False, "reason": "not_pending", "message": "待办不存在或非 pending"}
        return {"ok": True, "cancelled": int(sid)}

    @app.post("/api/care/schedule/{sid}/send-now")
    async def api_care_schedule_send_now(sid: int, request: Request, _=Depends(api_auth)):
        """立即发：把 due_at 提前到当前 → 下个派发 tick 即到期处理（仍走全套发送护栏）。"""
        store = _store(request)
        ok = store.bring_forward(int(sid))
        if not ok:
            return {"ok": False, "reason": "not_pending", "message": "待办不存在或非 pending"}
        return {"ok": True, "due_now": int(sid)}

    # ── Phase O 质量闭环：care dry_run 样本审核（与 reactivation 同范式）────────
    @app.get("/api/care/dry-run-samples")
    async def api_care_dry_samples(
        request: Request, limit: int = 50, before_ts: float = 0, _=Depends(api_auth),
    ):
        """care_dispatcher dry_run 模式下最近生成的关怀话术样本（供运营审核）。"""
        try:
            from src.monitoring.metrics_store import get_metrics_store
            samples = get_metrics_store().care_dry_samples(
                limit=max(1, min(int(limit or 50), 200)),
                before_ts=before_ts if before_ts > 0 else None,
            )
            return {"ok": True, "count": len(samples), "samples": samples}
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}:{ex}"}

    @app.post("/api/care/dry-run-feedback")
    async def api_care_dry_feedback(request: Request, _=Depends(api_auth)):
        """对 care dry_run 样本的人工反馈。body：{sample_ts, verdict:like|dislike}。
        dislike → reply_text 进**共享** dislike 黑名单（care/reactivation 都会规避）。"""
        try:
            body = await request.json()
        except Exception:
            body = {}
        verdict = str(body.get("verdict", "")).strip().lower()
        if verdict not in ("like", "dislike"):
            return {"ok": False, "reason": "bad_verdict", "message": "verdict 须为 like/dislike"}
        sample_ts = float(body.get("sample_ts") or 0)
        try:
            from src.monitoring.metrics_store import get_metrics_store
            ms = get_metrics_store()
            ms.record_care_feedback(verdict)  # O·P 联动质量看板计数
            if verdict == "dislike" and sample_ts > 0:
                for s in ms.care_dry_samples(limit=200):
                    if abs(float(s.get("ts") or 0) - sample_ts) < 1.0:
                        ms.add_disliked_reply(s.get("reply_text", ""))
                        break
        except Exception:
            pass
        return {"ok": True, "verdict": verdict, "sample_ts": sample_ts}


__all__ = ["register_care_routes"]
