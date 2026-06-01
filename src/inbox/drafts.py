"""DraftService — 统一草稿/审批层（Phase B）。

read-through 聚合：实时直读各平台源表，归一成 UnifiedDraft；reply_drafts 表
只存 inbox 自发草稿与风险 overlay，不镜像平台草稿（避免陈旧一致性问题）。

resolve 派发：统一 draft_id = "{source_kind}:{source_id}"，反解后路由到对应
source adapter，由 adapter 翻译成平台原生 resolve 调用（runner 行为不变）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .draft_models import (
    LinePendingAdapter,
    MessengerApprovalAdapter,
    WhatsAppPendingAdapter,
    UnifiedDraft,
)

logger = logging.getLogger(__name__)


class DraftService:
    def __init__(
        self,
        *,
        inbox_store: Optional[Any] = None,
        line_services: Optional[List[Any]] = None,
        wa_services: Optional[List[Any]] = None,
        messenger_service: Optional[Any] = None,
    ) -> None:
        self._store = inbox_store
        self._adapters = [
            LinePendingAdapter(line_services or []),
            WhatsAppPendingAdapter(wa_services or []),
            MessengerApprovalAdapter(messenger_service),
        ]
        self._by_kind = {a.source_kind: a for a in self._adapters}

    # ── 读：跨平台统一列表（read-through）─────────────────────

    def list_drafts(
        self, *, status: str = "pending", platform: str = "", limit: int = 50
    ) -> List[Dict[str, Any]]:
        drafts: List[UnifiedDraft] = []
        for adapter in self._adapters:
            if platform and adapter.platform != platform:
                continue
            try:
                drafts.extend(adapter.list_drafts(status=status, limit=limit))
            except Exception:
                logger.debug("source adapter %s 列举失败", adapter.source_kind, exc_info=True)
        # inbox 自发草稿（无平台表，存在 reply_drafts）
        if (not platform or platform == "inbox") and self._store is not None:
            try:
                for row in self._store.list_drafts(source_kind="inbox", status=status, limit=limit):
                    drafts.append(_row_to_unified(row))
            except Exception:
                logger.debug("inbox 自发草稿列举失败", exc_info=True)
        # 风险 overlay 合并：平台草稿挂上 reply_drafts 里的 risk 元数据
        self._merge_overlays(drafts)
        drafts.sort(key=lambda d: d.created_ts or 0, reverse=True)
        return [d.to_dict() for d in drafts[:limit]]

    def _merge_overlays(self, drafts: List[UnifiedDraft]) -> None:
        if self._store is None:
            return
        for d in drafts:
            if d.source_kind == "inbox":
                continue
            try:
                ov = self._store.get_overlay(d.source_kind, d.source_id)
            except Exception:
                ov = None
            if ov:
                d.risk_level = ov.get("risk_level") or d.risk_level
                d.risk_reasons = ov.get("risk_reasons") or d.risk_reasons
                d.autopilot_level = ov.get("autopilot_level") or d.autopilot_level
                d.translated_preview = ov.get("translated_preview") or d.translated_preview

    def get_draft(self, draft_id: str) -> Optional[Dict[str, Any]]:
        kind, _, sid = str(draft_id or "").partition(":")
        if kind == "inbox" and self._store is not None:
            return self._store.get_draft(draft_id)
        adapter = self._by_kind.get(kind)
        if adapter is None:
            return None
        for d in adapter.list_drafts(status="", limit=200):
            if d.source_id == sid:
                self._merge_overlays([d])
                return d.to_dict()
        return None

    # ── 写：统一 resolve 派发 ─────────────────────────────────

    def resolve(self, draft_id: str, action: str, *, text: str = "", by: str = "") -> Dict[str, Any]:
        kind, _, sid = str(draft_id or "").partition(":")
        action = str(action or "").strip().lower()
        if action not in {"approve", "reject", "edit_send", "cancel"}:
            return {"ok": False, "error": f"不支持的动作: {action}", "code": 400}
        adapter = self._by_kind.get(kind)
        if adapter is None:
            return {"ok": False, "error": f"未知草稿来源: {kind}", "code": 400}
        result = adapter.resolve(sid, action, text=text, by=by)
        # 同步 overlay 状态（best-effort，便于审计/SLA）
        if result.get("ok") and self._store is not None:
            try:
                self._store.upsert_draft({
                    "source_kind": kind, "source_id": sid,
                    "platform": adapter.platform,
                    "status": _action_to_status(action),
                    "final_text": text or "",
                    "decided_by": by,
                })
            except Exception:
                logger.debug("overlay 状态同步失败", exc_info=True)
        return result

    # ── 风险 overlay 写入（接 Phase C1 分析结果）───────────────

    def apply_analysis(
        self, draft_id: str, analysis: Dict[str, Any], *, automation_mode: str = "review"
    ) -> Dict[str, Any]:
        """把 ChatAnalysis 风险结果写进草稿 overlay，并计算 autopilot_level。

        analysis: ChatAnalysis.to_dict() 形态（risk_level/risk_reasons/...）。
        返回 {ok, autopilot_level, autosend_allowed}。
        """
        if self._store is None:
            return {"ok": False, "error": "no store"}
        kind, _, sid = str(draft_id or "").partition(":")
        risk_level = str(analysis.get("risk_level") or "low")
        autopilot = risk_to_autopilot(risk_level, automation_mode)
        try:
            self._store.upsert_draft({
                "source_kind": kind, "source_id": sid,
                "platform": (self._by_kind.get(kind).platform if kind in self._by_kind else ""),
                "risk_level": risk_level,
                "risk_reasons": analysis.get("risk_reasons") or [],
                "autopilot_level": autopilot,
                "translated_preview": str(analysis.get("translated_preview") or ""),
                "status": "pending",
            })
        except Exception:
            logger.debug("apply_analysis overlay 写入失败", exc_info=True)
            return {"ok": False, "error": "overlay write failed"}
        return {
            "ok": True,
            "autopilot_level": autopilot,
            "autosend_allowed": is_autosend_allowed(risk_level, automation_mode),
        }

    # ── 统计 ─────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        by_platform: Dict[str, Dict[str, int]] = {}
        total_pending = 0
        for adapter in self._adapters:
            try:
                pend = adapter.list_drafts(status="pending", limit=500)
            except Exception:
                pend = []
            by_platform[adapter.platform] = {"pending": len(pend)}
            total_pending += len(pend)
        return {"total_pending": total_pending, "by_platform": by_platform}


def _action_to_status(action: str) -> str:
    return {
        "approve": "approved", "edit_send": "approved",
        "reject": "rejected", "cancel": "cancelled",
    }.get(action, "pending")


# ── 风险分层（L0–L4）：接 Phase C1 的 ChatAnalysis.risk_level ──────────

_HIGH = "high"
_MEDIUM = "medium"


def risk_to_autopilot(risk_level: str, automation_mode: str) -> str:
    """把风险等级 + 自动化模式映射到 L0–L4。

    L0 仅翻译(manual) / L1 草稿待审(默认/review) / L2 低风险自动(auto_ai+low) /
    L3 中风险审批(medium) / L4 高风险人工(high)。
    """
    risk = str(risk_level or "low").lower()
    mode = str(automation_mode or "review").lower()
    if risk == _HIGH:
        return "L4"
    if risk == _MEDIUM:
        return "L3"
    if mode == "manual":
        return "L0"
    if mode == "auto_ai":
        return "L2"
    return "L1"


def is_autosend_allowed(risk_level: str, automation_mode: str) -> bool:
    """是否允许自动发送。核心安全不变量：medium/high 一律禁止自动发，
    即使 automation_mode=auto_ai。仅 L2（低风险 + auto_ai）放行。"""
    return risk_to_autopilot(risk_level, automation_mode) == "L2"


def _row_to_unified(row: Dict[str, Any]) -> UnifiedDraft:
    return UnifiedDraft(
        draft_id=str(row.get("draft_id") or ""),
        source_kind=str(row.get("source_kind") or "inbox"),
        source_id=str(row.get("source_id") or ""),
        platform=str(row.get("platform") or ""),
        account_id=str(row.get("account_id") or "default"),
        chat_key=str(row.get("chat_key") or ""),
        conversation_id=str(row.get("conversation_id") or ""),
        peer_text=str(row.get("peer_text") or ""),
        draft_text=str(row.get("final_text") or row.get("draft_text") or ""),
        draft_lang=str(row.get("draft_lang") or ""),
        status=str(row.get("status") or "pending"),
        created_ts=float(row.get("created_at") or 0),
        decided_by=str(row.get("decided_by") or ""),
        risk_level=str(row.get("risk_level") or "low"),
        risk_reasons=row.get("risk_reasons") or [],
        autopilot_level=str(row.get("autopilot_level") or ""),
        translated_preview=str(row.get("translated_preview") or ""),
    )
