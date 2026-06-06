"""DraftService — 统一草稿/审批层（Phase B）。

read-through 聚合：实时直读各平台源表，归一成 UnifiedDraft；reply_drafts 表
只存 inbox 自发草稿与风险 overlay，不镜像平台草稿（避免陈旧一致性问题）。

resolve 派发：统一 draft_id = "{source_kind}:{source_id}"，反解后路由到对应
source adapter，由 adapter 翻译成平台原生 resolve 调用（runner 行为不变）。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from .draft_models import (
    LinePendingAdapter,
    MessengerApprovalAdapter,
    WhatsAppPendingAdapter,
    UnifiedDraft,
)

logger = logging.getLogger(__name__)

# ── B2 敏感关键词强制升级表（不依赖 LLM，规则层兜底）─────────────────
# 格式：(pattern, forced_risk_level)  — 按顺序匹配，首中即止
# high → L4 强制拦截；medium → L3 必须审批
_SENSITIVE_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # L4: 支付/账号安全/直接要钱
    (re.compile(
        r"(退款|refund|退钱|付款|支付|payment|转账|wire\s*transfer|银行卡|card.*number"
        r"|密码|password|账号密码|account.*password|验证码|otp|二维码.*付)",
        re.IGNORECASE,
    ), "high"),
    # L3: 优惠/折扣/投诉/敏感服务
    (re.compile(
        r"(优惠|折扣|discount|coupon|免费|free.*shipping|投诉|complaint|律师|法律|起诉"
        r"|骗|scam|fraud|报警|police)",
        re.IGNORECASE,
    ), "medium"),
]


def keyword_risk_level(text: str) -> Optional[str]:
    """检测文本是否命中敏感关键词，返回强制 risk_level（high/medium）或 None。

    仅负责升级（不降级）：若已有 LLM 判定，调用方自行取 max。
    """
    t = str(text or "")
    for pattern, level in _SENSITIVE_PATTERNS:
        if pattern.search(t):
            return level
    return None


def _max_risk(a: str, b: Optional[str]) -> str:
    """取两个 risk_level 中更高的（high > medium > low > unknown）。"""
    _RANK = {"high": 3, "medium": 2, "low": 1, "unknown": 0}
    ra = _RANK.get(str(a or "unknown").lower(), 0)
    rb = _RANK.get(str(b or "unknown").lower(), 0)
    if ra >= rb:
        return str(a or "unknown")
    return str(b)


class DraftService:
    def __init__(
        self,
        *,
        inbox_store: Optional[Any] = None,
        line_services: Optional[List[Any]] = None,
        wa_services: Optional[List[Any]] = None,
        messenger_service: Optional[Any] = None,
        risk_fn: Optional[Any] = None,
    ) -> None:
        self._store = inbox_store
        # 同步零成本规则风险函数 risk_fn(text)->(level, reasons)（P0-c）；
        # 用于给无 overlay 的草稿实时算风险徽章，不调 LLM。
        self._risk_fn = risk_fn
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
        # 无 overlay 风险的草稿：用同步规则函数现算（零成本，不调 LLM）
        self._apply_quick_risk(drafts)
        drafts.sort(key=lambda d: d.created_ts or 0, reverse=True)
        return [d.to_dict() for d in drafts[:limit]]

    def _apply_quick_risk(self, drafts: List[UnifiedDraft]) -> None:
        """对仍无明确风险（overlay 未覆盖）的草稿，用规则函数现算 risk + autopilot。"""
        if self._risk_fn is None:
            return
        for d in drafts:
            if d.risk_level and d.risk_level != "unknown":
                continue  # 已有 overlay/LLM 风险，不覆盖
            text = d.peer_text or d.draft_text
            if not text:
                continue
            try:
                level, reasons = self._risk_fn(text)
            except Exception:
                continue
            d.risk_level = level or "low"
            if reasons and not d.risk_reasons:
                d.risk_reasons = list(reasons)
            if not d.autopilot_level:
                d.autopilot_level = risk_to_autopilot(d.risk_level, "review")

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

    # ── B2 强制风险执行 + 审计闭环 ───────────────────────────────

    def resolve_with_audit(
        self,
        draft_id: str,
        action: str,
        *,
        text: str = "",
        by: str = "",
        force_override: bool = False,
    ) -> Dict[str, Any]:
        """带 L4 强制拦截 + 审计的统一处置入口（替代裸 resolve）。

        安全不变量：
          - L4（high risk）：approve/edit_send 被强制拦截，写 blocked 审计；
            除非 force_override=True（主管专属）。
          - L2（auto_ai + low）：autosend 动作直接走 approve，写 autosend 审计。
          - L3/L4 的所有正常审批也写审计，保证不漏记。
          - 关键词强制升级：peer_text/draft_text 命中敏感词时 risk 升级（不降级）。
        """
        action = str(action or "").strip().lower()
        # 获取当前草稿（含 overlay 风险数据）
        draft = self.get_draft(draft_id)
        if draft is None:
            return {"ok": False, "error": "草稿不存在", "code": 404}

        # 关键词强制升级 risk（peer_text 或 draft_text 命中则升 risk + autopilot）
        kw_risk = keyword_risk_level(
            str(draft.get("peer_text") or "") + " " + str(draft.get("draft_text") or "")
        )
        base_risk = str(draft.get("risk_level") or "unknown")
        effective_risk = _max_risk(base_risk, kw_risk)
        if kw_risk and effective_risk != base_risk:
            # 实时更新 overlay（best-effort）
            autopilot_from_kw = risk_to_autopilot(
                effective_risk,
                draft.get("automation_mode") or "review",
            )
            try:
                if self._store is not None:
                    kind, _, sid = str(draft_id or "").partition(":")
                    self._store.upsert_draft({
                        "source_kind": kind, "source_id": sid,
                        "risk_level": effective_risk,
                        "autopilot_level": autopilot_from_kw,
                        "status": "pending",
                    })
            except Exception:
                logger.debug("keyword risk overlay write failed", exc_info=True)
            draft["risk_level"] = effective_risk
            draft["autopilot_level"] = autopilot_from_kw

        autopilot = str(draft.get("autopilot_level") or "L1")
        conv_id = str(draft.get("conversation_id") or "")

        # ── L4 强制拦截 ──
        if autopilot == "L4" and action in {"approve", "edit_send", "autosend"}:
            if not force_override:
                self._write_audit(
                    draft_id, autopilot, "blocked", by,
                    reason="L4 high-risk blocked (no force_override)",
                    risk_level=effective_risk,
                    conversation_id=conv_id,
                )
                return {
                    "ok": False,
                    "error": "L4 高风险草稿已被强制拦截，需主管强制放行",
                    "code": 422,
                    "autopilot_level": "L4",
                    "blocked": True,
                }
            # force_override 路径
            self._write_audit(
                draft_id, autopilot, "force_override", by,
                reason="supervisor forced override of L4 block",
                risk_level=effective_risk,
                conversation_id=conv_id,
            )

        # ── 常规审批 (L3/L4 force_override) 写审计 ──
        elif autopilot in {"L3", "L4"} or action == "autosend":
            audit_action = "autosend" if action == "autosend" else action
            self._write_audit(
                draft_id, autopilot, audit_action, by,
                risk_level=effective_risk,
                conversation_id=conv_id,
            )

        # autosend 转为 approve 下发
        real_action = "approve" if action == "autosend" else action

        result = self.resolve(draft_id, real_action, text=text, by=by)
        return result

    def _write_audit(
        self,
        draft_id: str,
        autopilot_level: str,
        action: str,
        agent_id: str,
        *,
        reason: str = "",
        risk_level: str = "",
        conversation_id: str = "",
    ) -> None:
        if self._store is None:
            return
        try:
            self._store.record_draft_audit(
                draft_id,
                autopilot_level=autopilot_level,
                action=action,
                agent_id=agent_id,
                reason=reason,
                risk_level=risk_level,
                conversation_id=conversation_id,
            )
        except Exception:
            logger.debug("draft audit write failed", exc_info=True)

    def list_audit(
        self,
        *,
        draft_id: str = "",
        agent_id: str = "",
        since_ts: float = 0.0,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """查审计日志（透传到 store.list_draft_audit）。"""
        if self._store is None:
            return []
        try:
            return self._store.list_draft_audit(
                draft_id=draft_id, agent_id=agent_id,
                since_ts=since_ts, limit=limit,
            )
        except Exception:
            logger.debug("list_audit failed", exc_info=True)
            return []

    def risk_summary(self) -> Dict[str, Any]:
        """按 autopilot_level 统计待处理草稿数（L0–L4 分布）。"""
        all_pending = self.list_drafts(status="pending", limit=500)
        counts: Dict[str, int] = {"L0": 0, "L1": 0, "L2": 0, "L3": 0, "L4": 0, "unknown": 0}
        for d in all_pending:
            lvl = str(d.get("autopilot_level") or "unknown")
            if lvl not in counts:
                lvl = "unknown"
            counts[lvl] += 1
        return {
            "total_pending": len(all_pending),
            "by_level": counts,
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
