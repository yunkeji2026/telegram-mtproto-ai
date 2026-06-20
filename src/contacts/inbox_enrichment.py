"""Phase R/R2/R3：inbox 跨域富集（contacts journey ↔ inbox conversation/meta）。

纯函数 + 批量编排：从候选 conversation_id 列表挑最近活跃会话，拼紧凑 inbox 块。
供单人健康卡（全字段）与流失预警榜（compact 列）共用，避免 routes 里重复逻辑。
R3：``parse_churn_level`` / ``inbox_sort_tiebreak_key`` / ``health_board_sort_key`` 供榜单次级排序。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional, Tuple

_CHURN_PRIO = {"high": 0, "medium": 1, "low": 2}
_EMOTION_PRIO = {"rising": 0, "stable": 1, "falling": 2}


def parse_churn_level(raw: Any) -> str:
    """从 conversation_meta.churn_risk 解析 level（支持 JSON 或裸字符串）。"""
    s = str(raw or "").strip()
    if not s:
        return ""
    if s.lower() in _CHURN_PRIO:
        return s.lower()
    try:
        d = json.loads(s)
        if isinstance(d, dict):
            return str(d.get("level") or "").strip().lower()
    except Exception:
        pass
    return s.lower()


def inbox_sort_tiebreak_key(inbox: Optional[Mapping[str, Any]]) -> Tuple[int, int]:
    """R3 次级排序键（越小越靠前）：高流失 / 情绪恶化优先。无 inbox → 最低优先级。"""
    if not inbox:
        return (2, 3)
    churn = parse_churn_level(inbox.get("churn_risk"))
    trend = str(inbox.get("emotion_trend") or "").strip().lower()
    return (_CHURN_PRIO.get(churn, 2), _EMOTION_PRIO.get(trend, 3))


def is_payer_at_risk(item: Mapping[str, Any]) -> bool:
    """付费/会员用户且处于 at_risk/critical——最该挽留（K2c①）。"""
    mb = item.get("monetization") or {}
    return bool(
        (mb.get("is_payer") or mb.get("is_member"))
        and item.get("risk_level") in ("at_risk", "critical"))


def health_board_sort_key(
    item: Mapping[str, Any],
    *,
    inbox_tiebreak: bool = False,
    payer_priority: bool = False,
) -> Tuple[Any, ...]:
    """流失预警榜排序键：（可选①）付费流失绝对置顶 → value_at_risk 优先 →
    健康分升序 →（可选 R3）inbox 次级 tie-break。"""
    base: Tuple[Any, ...] = (not item.get("value_at_risk"), item.get("score", 0))
    if payer_priority:
        base = (not is_payer_at_risk(item),) + base
    if not inbox_tiebreak:
        return base
    return base + inbox_sort_tiebreak_key(item.get("inbox"))


def pick_primary_conversation(
    conv_ids: List[str],
    conv_map: Mapping[str, Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], int]:
    """在候选 conv_id 中挑 last_ts 最新的一条；返回 (会话 dict, 命中数)。"""
    primary: Optional[Dict[str, Any]] = None
    matched = 0
    for cid in conv_ids:
        conv = conv_map.get(cid)
        if not conv:
            continue
        matched += 1
        if primary is None or float(conv.get("last_ts") or 0) > float(
                primary.get("last_ts") or 0):
            primary = conv
    return primary, matched


def build_inbox_block(
    primary: Dict[str, Any],
    meta: Mapping[str, Any],
    *,
    matched: int = 1,
    compact: bool = False,
) -> Dict[str, Any]:
    cid = str(primary.get("conversation_id") or "")
    churn = parse_churn_level(meta.get("churn_risk"))
    base: Dict[str, Any] = {
        "conversation_id": cid,
        "emotion_trend": str(meta.get("emotion_trend") or ""),
        "churn_risk": churn,
        "last_intent": str(meta.get("last_intent") or ""),
    }
    if compact:
        return base
    return {
        **base,
        "conversations_matched": matched,
        "last_ts": primary.get("last_ts") or 0,
        "unread": int(primary.get("unread") or 0),
        "last_text": str(primary.get("last_text") or "")[:80],
        "last_emotion": str(meta.get("last_emotion") or ""),
        "last_risk": str(meta.get("last_risk") or ""),
        "msg_count": int(meta.get("msg_count") or 0),
        "summary": str(meta.get("summary") or "")[:160],
    }


def inbox_enrichment_for_conv_ids(
    conv_ids: List[str],
    conv_map: Mapping[str, Dict[str, Any]],
    meta_map: Mapping[str, Dict[str, Any]],
    *,
    compact: bool = False,
) -> Optional[Dict[str, Any]]:
    primary, matched = pick_primary_conversation(conv_ids, conv_map)
    if primary is None:
        return None
    cid = str(primary.get("conversation_id") or "")
    meta = meta_map.get(cid) or {}
    return build_inbox_block(primary, meta, matched=matched, compact=compact)


def inbox_enrichment_batch_for_journeys(
    journey_ids: List[str],
    contact_by_jid: Mapping[str, str],
    convkeys_by_contact: Mapping[str, List[str]],
    inbox_store: Any,
    *,
    compact: bool = True,
) -> Dict[str, Optional[Dict[str, Any]]]:
    """对一批 journey 批量富集 inbox 语境（2 次 SQL：conversations + meta）。

    仅查上榜 journey 的 conv_id 并集，避免对全 scan 做 N+1。
    """
    if inbox_store is None or not journey_ids:
        return {jid: None for jid in journey_ids}
    jid_conv_ids: Dict[str, List[str]] = {}
    all_ids: List[str] = []
    for jid in journey_ids:
        contact_id = contact_by_jid.get(jid) or ""
        keys = convkeys_by_contact.get(contact_id, [])
        if keys:
            jid_conv_ids[jid] = keys
            all_ids.extend(keys)
    all_ids = list(dict.fromkeys(all_ids))
    if not all_ids:
        return {jid: None for jid in journey_ids}
    try:
        conv_map = inbox_store.get_conversations_for_ids(all_ids)
        meta_ids = list(conv_map.keys())
        meta_map = (
            inbox_store.get_conv_meta_for_ids(meta_ids) if meta_ids else {}
        )
    except Exception:
        return {jid: None for jid in journey_ids}
    return {
        jid: (
            inbox_enrichment_for_conv_ids(
                jid_conv_ids[jid], conv_map, meta_map, compact=compact)
            if jid in jid_conv_ids else None
        )
        for jid in journey_ids
    }


__all__ = [
    "parse_churn_level",
    "inbox_sort_tiebreak_key",
    "health_board_sort_key",
    "pick_primary_conversation",
    "build_inbox_block",
    "inbox_enrichment_for_conv_ids",
    "inbox_enrichment_batch_for_journeys",
]
