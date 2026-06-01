"""统一草稿模型 + 各平台 source adapter（Phase B）。

设计要点：
- 读路径 read-through：直读各平台源表（line_rpa_pending / wa_rpa_pending /
  messenger_rpa_approvals），不在 reply_drafts 落镜像 → 无陈旧一致性问题。
- resolve 派发：三个平台的 resolve API 签名分歧（实测）：
    LINE:      service.resolve_pending(id, *, action, final_reply=None, by="")
               动作 {approve, reject, edit_approve, cancel}
    WhatsApp:  service.resolve_pending(id, action, by="")   # 无文本编辑
               动作 {approve, reject, send}
    Messenger: state_store().decide_approval(id, *, approve: bool, decided_by="")
               + update_approval_reply(id, reply_text=...) + service.send_approved_now(id)
  各 adapter 把统一动作（approve/reject/edit_send/cancel）翻译成平台原生调用。
- 统一 draft_id = "{source_kind}:{source_id}"，其中 source_id = "{account_id}:{raw_id}"，
  即 "line_pending:line-a:11"。account_id 内嵌确保多账号精确路由 + overlay 不串号。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 统一状态词汇
UNIFIED_STATUSES = {"pending", "approved", "rejected", "sent", "failed", "cancelled", "expired"}

# 各源状态词汇 → 统一 status
STATUS_MAP: Dict[str, Dict[str, str]] = {
    "line_pending": {
        "pending": "pending", "approved": "approved", "rejected": "rejected",
        "sent": "sent", "cancelled": "cancelled", "error": "failed",
    },
    "wa_pending": {
        "pending": "pending", "approved": "approved", "rejected": "rejected",
        "sent": "sent", "error": "failed",
    },
    "messenger_approval": {
        "pending": "pending", "approved": "approved", "rejected": "rejected",
        "sent": "sent", "failed": "failed", "deferred": "pending",
        "expired": "expired",
    },
}


def map_status(source_kind: str, raw_status: str) -> str:
    raw = str(raw_status or "").strip().lower()
    return STATUS_MAP.get(source_kind, {}).get(raw, raw or "pending")


@dataclass
class UnifiedDraft:
    """跨平台统一草稿 DTO（读路径产物）。"""

    draft_id: str
    source_kind: str
    source_id: str
    platform: str
    account_id: str = "default"
    account_label: str = ""
    chat_key: str = ""
    chat_name: str = ""
    conversation_id: str = ""
    peer_text: str = ""
    draft_text: str = ""
    draft_lang: str = ""
    status: str = "pending"
    created_ts: float = 0.0
    decided_by: str = ""
    # 风险 overlay（Phase C 填；read-through 默认占位）
    risk_level: str = "unknown"
    risk_reasons: List[str] = field(default_factory=list)
    autopilot_level: str = ""
    translated_preview: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "platform": self.platform,
            "account_id": self.account_id,
            "account_label": self.account_label,
            "chat_key": self.chat_key,
            "chat_name": self.chat_name,
            "conversation_id": self.conversation_id,
            "peer_text": self.peer_text,
            "draft_text": self.draft_text,
            "draft_lang": self.draft_lang,
            "status": self.status,
            "created_ts": self.created_ts,
            "decided_by": self.decided_by,
            "risk_level": self.risk_level,
            "risk_reasons": list(self.risk_reasons),
            "autopilot_level": self.autopilot_level,
            "translated_preview": self.translated_preview,
        }


def _conv_id(platform: str, account_id: str, chat_key: str) -> str:
    return f"{platform}:{account_id}:{chat_key}"


def make_source_id(account_id: str, raw_id: Any) -> str:
    """source_id = "{account_id}:{raw_id}"，account 内嵌以支持多账号精确路由。"""
    return f"{account_id}:{raw_id}"


def split_source_id(source_id: str) -> "tuple[str, str]":
    """反解 source_id → (account_id, raw_id)。无冒号时 account_id 为空。"""
    account_id, sep, raw_id = str(source_id or "").partition(":")
    return (account_id, raw_id) if sep else ("", account_id)


def _label_of(svc: Any, account_id: str) -> str:
    try:
        cfg = getattr(svc, "_merged_cfg", {}) or {}
        return cfg.get("label") or account_id
    except Exception:
        return account_id


# ── Source Adapters ──────────────────────────────────────────────────

class _BaseSourceAdapter:
    source_kind = ""
    platform = ""

    def list_drafts(self, *, status: str = "pending", limit: int = 50) -> List[UnifiedDraft]:
        raise NotImplementedError

    def resolve(self, source_id: str, action: str, *, text: str = "", by: str = "") -> Dict[str, Any]:
        raise NotImplementedError

    @staticmethod
    def _ok(payload: Any = None) -> Dict[str, Any]:
        return {"ok": True, "result": payload}

    @staticmethod
    def _err(msg: str, code: int = 500) -> Dict[str, Any]:
        return {"ok": False, "error": msg, "code": code}


class LinePendingAdapter(_BaseSourceAdapter):
    source_kind = "line_pending"
    platform = "line"

    def __init__(self, services: List[Any]):
        self._services = [s for s in (services or []) if s is not None]

    def _svc_for(self, account_id: str) -> Optional[Any]:
        for s in self._services:
            if str(getattr(s, "account_id", "default")) == str(account_id):
                return s
        return self._services[0] if self._services else None

    def list_drafts(self, *, status: str = "pending", limit: int = 50) -> List[UnifiedDraft]:
        out: List[UnifiedDraft] = []
        for svc in self._services:
            aid = str(getattr(svc, "account_id", "default"))
            label = _label_of(svc, aid)
            try:
                rows = svc.list_pending(status=status or None, limit=limit) or []
            except Exception as ex:
                logger.debug("LINE list_pending [%s] 失败: %s", aid, ex)
                continue
            for r in rows:
                sid = make_source_id(aid, r.get("id") or "")
                chat_key = str(r.get("chat_key") or "")
                out.append(UnifiedDraft(
                    draft_id=f"{self.source_kind}:{sid}",
                    source_kind=self.source_kind, source_id=sid,
                    platform=self.platform, account_id=aid, account_label=label,
                    chat_key=chat_key, chat_name=str(r.get("chat_name") or ""),
                    conversation_id=_conv_id(self.platform, aid, chat_key),
                    peer_text=str(r.get("peer_text") or ""),
                    draft_text=str(r.get("final_reply") or r.get("draft_reply") or ""),
                    draft_lang=str(r.get("forced_lang") or ""),
                    status=map_status(self.source_kind, r.get("status")),
                    created_ts=float(r.get("ts") or 0),
                    decided_by=str(r.get("resolved_by") or ""),
                ))
        return out

    def resolve(self, source_id: str, action: str, *, text: str = "", by: str = "") -> Dict[str, Any]:
        line_action = {"approve": "approve", "reject": "reject",
                       "edit_send": "edit_approve", "cancel": "cancel"}.get(action, action)
        account_id, raw_id = split_source_id(source_id)
        try:
            pid = int(raw_id)
        except (TypeError, ValueError):
            return self._err(f"非法 source_id: {source_id}", 400)
        svc = self._svc_for(account_id)
        if svc is None:
            return self._err("LINE 服务未启用", 503)
        try:
            res = svc.resolve_pending(
                pid, action=line_action, final_reply=(text or None), by=by,
            )
        except TypeError:
            res = svc.resolve_pending(pid, action=line_action, by=by)
        except Exception as ex:
            logger.debug("LINE resolve_pending 失败: %s", ex)
            return self._err(str(ex), 500)
        if res:
            return self._ok(res)
        return self._err("未找到该草稿", 404)


class WhatsAppPendingAdapter(_BaseSourceAdapter):
    source_kind = "wa_pending"
    platform = "whatsapp"

    def __init__(self, services: List[Any]):
        self._services = [s for s in (services or []) if s is not None]

    def _svc_for(self, account_id: str) -> Optional[Any]:
        for s in self._services:
            if str(getattr(s, "account_id", "default")) == str(account_id):
                return s
        return self._services[0] if self._services else None

    def list_drafts(self, *, status: str = "pending", limit: int = 50) -> List[UnifiedDraft]:
        out: List[UnifiedDraft] = []
        for svc in self._services:
            aid = str(getattr(svc, "account_id", "default"))
            label = _label_of(svc, aid)
            try:
                rows = svc.list_pending(status=status or None, limit=limit) or []
            except Exception as ex:
                logger.debug("WA list_pending [%s] 失败: %s", aid, ex)
                continue
            for r in rows:
                sid = make_source_id(aid, r.get("id") or "")
                chat_key = str(r.get("chat_key") or "")
                out.append(UnifiedDraft(
                    draft_id=f"{self.source_kind}:{sid}",
                    source_kind=self.source_kind, source_id=sid,
                    platform=self.platform, account_id=aid, account_label=label,
                    chat_key=chat_key, chat_name=str(r.get("peer_name") or ""),
                    conversation_id=_conv_id(self.platform, aid, chat_key),
                    peer_text=str(r.get("peer_text") or ""),
                    draft_text=str(r.get("proposed_reply") or ""),
                    status=map_status(self.source_kind, r.get("status")),
                    created_ts=float(r.get("ts") or 0),
                    decided_by=str(r.get("resolved_by") or ""),
                ))
        return out

    def resolve(self, source_id: str, action: str, *, text: str = "", by: str = "") -> Dict[str, Any]:
        # WhatsApp resolve 不支持文本编辑；edit_send 退化为 approve（忽略 text 并提示）
        wa_action = {"approve": "approve", "reject": "reject",
                     "edit_send": "approve", "cancel": "reject"}.get(action, action)
        if wa_action not in {"approve", "reject", "send"}:
            return self._err(f"WhatsApp 不支持动作: {action}", 400)
        account_id, raw_id = split_source_id(source_id)
        try:
            pid = int(raw_id)
        except (TypeError, ValueError):
            return self._err(f"非法 source_id: {source_id}", 400)
        svc = self._svc_for(account_id)
        if svc is None:
            return self._err("WhatsApp 服务未启用", 503)
        note = "wa_no_text_edit" if (action == "edit_send" and text) else ""
        try:
            res = svc.resolve_pending(pid, wa_action, by=by)
        except Exception as ex:
            logger.debug("WA resolve_pending 失败: %s", ex)
            return self._err(str(ex), 500)
        if res:
            payload = dict(res) if isinstance(res, dict) else {"result": res}
            if note:
                payload["note"] = note
            return self._ok(payload)
        return self._err("未找到该草稿", 404)


class MessengerApprovalAdapter(_BaseSourceAdapter):
    source_kind = "messenger_approval"
    platform = "messenger"

    def __init__(self, service: Any):
        self._svc = service

    def _store(self):
        svc = self._svc
        if svc is None:
            return None
        store = getattr(svc, "state_store", None)
        if callable(store):
            try:
                return store()
            except Exception:
                return None
        return store

    def list_drafts(self, *, status: str = "pending", limit: int = 50) -> List[UnifiedDraft]:
        if self._svc is None:
            return []
        rows = []
        # 优先 service.list_approvals，其次 state_store().list_approvals
        for target in (self._svc, self._store()):
            fn = getattr(target, "list_approvals", None) if target is not None else None
            if callable(fn):
                try:
                    rows = fn(status=status or None, limit=limit) or []
                    break
                except TypeError:
                    try:
                        rows = fn(status=status or None) or []
                        break
                    except Exception:
                        continue
                except Exception:
                    continue
        out: List[UnifiedDraft] = []
        for r in rows:
            aid = str(r.get("account_id") or "default")
            sid = make_source_id(aid, r.get("id") or "")
            chat_key = str(r.get("chat_key") or "")
            out.append(UnifiedDraft(
                draft_id=f"{self.source_kind}:{sid}",
                source_kind=self.source_kind, source_id=sid,
                platform=self.platform, account_id=aid, account_label=aid or "Messenger",
                chat_key=chat_key, chat_name=str(r.get("chat_name") or r.get("name") or ""),
                conversation_id=_conv_id(self.platform, aid, chat_key),
                peer_text=str(r.get("peer_text") or ""),
                draft_text=str(r.get("reply_text") or ""),
                draft_lang=str(r.get("reply_lang") or ""),
                status=map_status(self.source_kind, r.get("status")),
                created_ts=float(r.get("created_at") or 0),
                decided_by=str(r.get("decided_by") or ""),
            ))
        return out

    def resolve(self, source_id: str, action: str, *, text: str = "", by: str = "") -> Dict[str, Any]:
        if self._svc is None:
            return self._err("Messenger 服务未启用", 503)
        store = self._store()
        if store is None:
            return self._err("Messenger state_store 不可用", 503)
        _, raw_id = split_source_id(source_id)
        try:
            aid = int(raw_id)
        except (TypeError, ValueError):
            return self._err(f"非法 source_id: {source_id}", 400)

        try:
            if action in {"approve", "edit_send"}:
                if action == "edit_send" and text:
                    upd = getattr(store, "update_approval_reply", None)
                    if callable(upd):
                        upd(aid, reply_text=text)
                res = store.decide_approval(aid, approve=True, decided_by=by)
                # 尽快投递（service 支持时）
                send_now = getattr(self._svc, "send_approved_now", None)
                if callable(send_now):
                    return {"ok": True, "result": res, "deferred_send": True}
                return self._ok(res)
            if action in {"reject", "cancel"}:
                res = store.decide_approval(aid, approve=False, decided_by=by)
                return self._ok(res)
            return self._err(f"Messenger 不支持动作: {action}", 400)
        except Exception as ex:
            logger.debug("Messenger decide_approval 失败: %s", ex)
            return self._err(str(ex), 500)
