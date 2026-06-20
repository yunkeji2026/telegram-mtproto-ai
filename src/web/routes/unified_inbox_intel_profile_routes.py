"""统一收件箱——对话智能元数据 / 客户画像聚合 API 路由域（巨石拆分 slice 28）。

把 ``register_unified_inbox_routes`` 巨型闭包中相邻的两段 ``/api/unified-inbox/*`` 子域
整体外移为 ``register_intel_profile_routes(app, *, api_auth)``，由主 register 在**原位置**
调用：

- I1 对话智能元数据 API：``unified-inbox/conv-meta``
- K3 客户画像聚合 API：``unified-inbox/contact-profile``（对话智能 + CRM 档案 +
  草稿决策 + 跨平台会话 + 摘要）

端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫 + slice 28 端点契约断言）。

依赖全部朝下：services.(_inbox_store/_contacts_store)。只收 api_auth 一个参数
（K3 画像在 handler 内联聚合，零闭包私有 helper）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import Depends, HTTPException, Request

from src.web.routes.unified_inbox_services import (
    _contacts_store,
    _inbox_store,
    _skill_manager,
)

logger = logging.getLogger(__name__)


def register_intel_profile_routes(app, *, api_auth) -> None:
    """挂载对话智能元数据 + 客户画像聚合端点。"""

    # ── I1 对话智能元数据 API ──────────────────────────────────────

    @app.get("/api/unified-inbox/conv-meta")
    async def api_conv_meta(request: Request, conversation_id: str = ""):
        """I1：获取对话智能元数据（最近意图、情绪趋势、风险、历史窗口）。

        返回：{ok, found, meta: {last_intent, last_emotion, emotion_trend,
                                  last_risk, msg_count, intent_history,
                                  emotion_history, updated_at}}
        """
        api_auth(request)
        store = _inbox_store(request)
        cid = str(conversation_id or "").strip()
        if not cid:
            raise HTTPException(400, "conversation_id 不能为空")
        if store is None:
            return {"ok": True, "found": False, "conversation_id": cid, "meta": None}
        meta = store.get_conv_meta(cid)
        return {
            "ok": True,
            "found": meta is not None,
            "conversation_id": cid,
            "meta": meta,
        }

    # ── K3：客户画像聚合 API ───────────────────────────────────────

    @app.get("/api/unified-inbox/contact-profile")
    async def api_contact_profile(
        request: Request,
        conversation_id: str = "",
        _=Depends(api_auth),
    ):
        """K3：聚合客户画像数据（对话智能 + CRM 档案 + 近期草稿决策）。

        返回：{ok, conversation_id, conv_meta, contact, recent_decisions}
        - conv_meta: 来自 I1 conversation_meta（最近意图/情绪/历史）
        - contact:   来自 contacts 子系统（标签/漏斗阶段/跟进状态）
        - recent_decisions: 来自 draft_audit_log（最近 5 条草稿决策记录）
        """
        cid = str(conversation_id or "").strip()
        if not cid:
            raise HTTPException(400, "conversation_id 不能为空")

        store = _inbox_store(request)
        result: Dict[str, Any] = {
            "ok": True,
            "conversation_id": cid,
            "conv_meta": None,
            "contact": None,
            "recent_decisions": [],
        }

        # ① 对话智能元数据（I1）
        if store is not None:
            try:
                result["conv_meta"] = store.get_conv_meta(cid)
            except Exception:
                pass

        # ② CRM 联系人档案（contacts 子系统，可选）
        try:
            _cstore = _contacts_store(request)
            if _cstore is not None:
                # 从 conversation_id 反推平台/chat_key（格式: platform:account:chat_key）
                parts = cid.split(":", 2)
                if len(parts) == 3:
                    platform_key, account_id_key, chat_key_key = parts
                    ci = _cstore.get_ci_by_external(platform_key, account_id_key, chat_key_key)
                    if ci is not None:
                        contact_id = str(ci.contact_id if hasattr(ci, "contact_id") else (ci.get("contact_id") or ""))
                        if contact_id:
                            contact = _cstore.get_contact(contact_id)
                            attrs = _cstore.get_contact_attributes(contact_id) or {}
                            if contact:
                                result["contact"] = {
                                    "contact_id": contact_id,
                                    "name": str(getattr(contact, "display_name", "") or contact.get("display_name") or ""),
                                    "tags": attrs.get("tags", []),
                                    "funnel_stage": attrs.get("funnel_stage") or attrs.get("funnel"),
                                    "follow_up_overdue": cid in (_cstore.overdue_contact_ids() or set()),
                                    "note": attrs.get("note") or attrs.get("notes") or "",
                                }
        except Exception:
            pass

        # ③ 近期草稿决策（draft_audit_log，最近 5 条）
        if store is not None:
            try:
                logs = store.list_draft_audit(limit=200)
                recent = [
                    {
                        "draft_id": row.get("draft_id", ""),
                        "action": row.get("action", ""),
                        "agent_id": row.get("agent_id", ""),
                        "risk_level": row.get("risk_level", ""),
                        "autopilot_level": row.get("autopilot_level", ""),
                        "ts": row.get("ts", 0),
                        "reason": row.get("reason", ""),
                    }
                    for row in logs
                    if str(row.get("conversation_id") or "") == cid
                ][:5]
                result["recent_decisions"] = recent
            except Exception:
                pass

        # ④ N1: 跨平台会话归档（同一 contact_id 的所有历史对话）
        if store is not None:
            try:
                conv_meta = result.get("conv_meta") or {}
                linked_contact_id = str(conv_meta.get("contact_id") or "")
                # 也尝试从 CRM contact 获取 contact_id
                if not linked_contact_id and result.get("contact"):
                    linked_contact_id = str(result["contact"].get("contact_id") or "")
                if linked_contact_id:
                    cross_sessions = store.get_contact_sessions(linked_contact_id, limit=20)
                    # 排除当前 conversation_id
                    cross_sessions = [s for s in cross_sessions if s.get("conversation_id") != cid]
                    contact_csat_avg = store.get_contact_csat_avg(linked_contact_id)
                    result["cross_platform"] = {
                        "contact_id": linked_contact_id,
                        "session_count": len(cross_sessions),
                        "sessions": cross_sessions[:10],  # 最多返回 10 条
                        "contact_csat_avg": contact_csat_avg,
                    }
                else:
                    result["cross_platform"] = None
            except Exception:
                result["cross_platform"] = None

        # ⑤ Q1: 对话摘要（最新生成的 summary，主管画像核心字段）
        if store is not None:
            try:
                _meta = result.get("conv_meta") or {}
                _summary = str(_meta.get("summary") or "").strip()
                result["conv_summary"] = _summary if _summary else None
            except Exception:
                result["conv_summary"] = None

        # ⑥ R9d/R9e: 危机概览（把 R9 安全链送到坐席手边，处置不切后台）
        result["crisis"] = None
        sm = None
        try:
            sm = _skill_manager(request)
            if sm is not None and hasattr(sm, "crisis_summary_for_user"):
                parts = cid.split(":", 2)
                chat_key_key = parts[2] if len(parts) == 3 else cid
                summary = sm.crisis_summary_for_user(chat_key_key, limit=5)
                # 仅在有危机记录时挂出，避免给侧栏添噪
                if summary and summary.get("total"):
                    result["crisis"] = summary
        except Exception:
            result["crisis"] = None

        # ⑦ R14: 记忆画像聚合（已知 N 条稳定事实 / M 条 AI 待确认推断）
        result["memory_profile"] = None
        try:
            if sm is not None and hasattr(sm, "episodic_profile_summary"):
                parts = cid.split(":", 2)
                chat_key_key = parts[2] if len(parts) == 3 else cid
                # 先按 chat_key（私聊=对端 user_id，episodic 主存储键），无则退回完整 cid
                summary = sm.episodic_profile_summary(chat_key_key, top_stable=3)
                if not summary.get("total"):
                    summary = sm.episodic_profile_summary(cid, top_stable=3)
                if summary and summary.get("total"):
                    result["memory_profile"] = summary
        except Exception:
            result["memory_profile"] = None

        return result
