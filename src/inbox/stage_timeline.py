"""P51 — 客户级关系阶段演进时间轴。"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from src.utils.companion_relationship import STAGE_LABEL_ZH, STAGE_ORDER

_STAGE_ACTIONS = frozenset({
    "stage_confirm", "stage_downgrade", "stage_reunion", "stage_sync",
})

_ACTION_LABELS = {
    "stage_confirm": "确认进阶",
    "stage_downgrade": "手动降级",
    "stage_reunion": "确认回暖",
    "stage_sync": "阶段对齐",
}

_ACTION_ICONS = {
    "stage_confirm": "⬆",
    "stage_downgrade": "⬇",
    "stage_reunion": "🌸",
    "stage_sync": "↔",
}

_LABEL_TO_STAGE = {v: k for k, v in STAGE_LABEL_ZH.items()}


def _resolve_stage(token: str) -> Tuple[str, str]:
    """将阶段 key 或中文标签解析为 (key, label)。"""
    t = str(token or "").strip()
    if not t:
        return "", ""
    if t in STAGE_ORDER:
        return t, STAGE_LABEL_ZH.get(t, t)
    if t in _LABEL_TO_STAGE:
        k = _LABEL_TO_STAGE[t]
        return k, STAGE_LABEL_ZH.get(k, t)
    return t, t


def _parse_arrow_transition(text: str) -> Tuple[str, str, str, str]:
    """解析「A → B」形式，返回 from_key, from_label, to_key, to_label。"""
    m = re.search(r"(.+?)\s*→\s*(.+?)(?:\s*：|$)", str(text or ""))
    if not m:
        return "", "", "", ""
    fk, fl = _resolve_stage(m.group(1).strip())
    tk, tl = _resolve_stage(m.group(2).strip())
    return fk, fl, tk, tl


def _parse_sync_reason(reason: str) -> Dict[str, Any]:
    m = re.search(
        r"对齐至\s+(\w+)\s*（(\w+)，(\d+)\s*会话）",
        str(reason or ""),
    )
    if not m:
        return {}
    stage, mode, synced = m.group(1), m.group(2), int(m.group(3))
    _, label = _resolve_stage(stage)
    return {
        "to_stage": stage,
        "to_label": label,
        "mode": mode,
        "synced": synced,
    }


def enrich_stage_audit_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """将 draft_audit_log 行 enrich 为时间轴事件。"""
    action = str(row.get("action") or "")
    reason = str(row.get("reason") or "")
    meta: Dict[str, Any] = {}

    if action == "stage_confirm":
        fk, fl, tk, tl = _parse_arrow_transition(reason)
        meta = {"from_stage": fk, "from_label": fl, "to_stage": tk, "to_label": tl}
        preview = f"{fl or '—'} → {tl or tk or '—'}" if (fl or tl) else reason
    elif action == "stage_downgrade":
        body = reason
        if body.startswith("[关系降级]"):
            body = body[len("[关系降级]"):].strip()
        fk, fl, tk, tl = _parse_arrow_transition(body)
        note = ""
        if "：" in body:
            note = body.split("：", 1)[1].strip()
        meta = {
            "from_stage": fk, "from_label": fl, "to_stage": tk, "to_label": tl,
            "note": note,
        }
        preview = f"{fl or '—'} → {tl or '—'}" + (f"：{note}" if note else "")
    elif action == "stage_reunion":
        body = reason
        if body.startswith("[关系回暖]"):
            body = body[len("[关系回暖]"):].strip()
        fk, fl, tk, tl = _parse_arrow_transition(body)
        note = body.split("：", 1)[1].strip() if "：" in body else reason
        meta = {
            "from_stage": fk, "from_label": fl, "to_stage": tk, "to_label": tl,
            "note": note,
        }
        preview = f"{fl or '—'} → {tl or '—'}" + (f"：{note}" if note else "")
    elif action == "stage_sync":
        meta = _parse_sync_reason(reason)
        tl = meta.get("to_label") or meta.get("to_stage") or ""
        preview = f"对齐至 {tl}" + (
            f"（{meta.get('synced', 0)} 会话）" if meta.get("synced") else ""
        )
    else:
        preview = reason

    agent_id = str(row.get("agent_id") or "")
    return {
        "event_type": action,
        "ts": float(row.get("ts") or 0),
        "agent_id": agent_id,
        "conversation_id": str(row.get("conversation_id") or ""),
        "platform": str(row.get("platform") or ""),
        "display_name": str(row.get("display_name") or ""),
        "label": _ACTION_LABELS.get(action, action),
        "icon": _ACTION_ICONS.get(action, "•"),
        "preview": preview or reason,
        "meta": meta,
    }


def build_contact_stage_summary(
    events: List[Dict[str, Any]],
    *,
    contact_rec: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """聚合阶段演进统计。"""
    confirms = sum(1 for e in events if e.get("event_type") == "stage_confirm")
    downgrades = sum(1 for e in events if e.get("event_type") == "stage_downgrade")
    reunions = sum(1 for e in events if e.get("event_type") == "stage_reunion")
    syncs = sum(1 for e in events if e.get("event_type") == "stage_sync")
    agents = sorted({str(e.get("agent_id") or "") for e in events if e.get("agent_id")})
    last_ts = max((float(e.get("ts") or 0) for e in events), default=0.0)

    cs = str((contact_rec or {}).get("confirmed_stage") or "")
    return {
        "current_stage": cs or None,
        "current_stage_label": STAGE_LABEL_ZH.get(cs, "") if cs else None,
        "updated_by": str((contact_rec or {}).get("updated_by") or "") or None,
        "updated_at": float((contact_rec or {}).get("updated_at") or 0) or None,
        "total_confirms": confirms,
        "total_downgrades": downgrades,
        "total_reunions": reunions,
        "total_syncs": syncs,
        "agent_ids": agents,
        "last_change_ts": last_ts or None,
    }
