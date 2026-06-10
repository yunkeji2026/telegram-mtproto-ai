"""P54 — Copilot 采纳率统计与回放解析。"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


def _parse_reason(raw: str) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {"_raw": raw}


def encode_impression(
    *,
    trigger: str = "",
    stage: str = "",
    polished: bool = False,
    suggestion_count: int = 0,
    top_source: str = "",
) -> str:
    return json.dumps({
        "trigger": trigger or "open",
        "stage": stage or "initial",
        "polished": bool(polished),
        "n": int(suggestion_count or 0),
        "top_source": top_source or "",
    }, ensure_ascii=False)


def encode_adopt(
    *,
    match: str = "exact",
    source: str = "",
    polished: bool = False,
    trigger: str = "",
    stage: str = "",
    suggested_preview: str = "",
    sent_preview: str = "",
) -> str:
    return json.dumps({
        "match": match or "exact",
        "source": source or "",
        "polished": bool(polished),
        "trigger": trigger or "",
        "stage": stage or "",
        "suggested": (suggested_preview or "")[:80],
        "sent": (sent_preview or "")[:80],
    }, ensure_ascii=False)


def aggregate_copilot_stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """从 draft_audit_log 行聚合 Copilot 指标。"""
    by_agent: Dict[str, Dict[str, Any]] = {}
    by_trigger: Dict[str, Dict[str, int]] = {}
    by_source: Dict[str, Dict[str, int]] = {}
    replays: List[Dict[str, Any]] = []

    for row in rows:
        action = str(row.get("action") or "")
        agent = str(row.get("agent_id") or "")
        meta = _parse_reason(str(row.get("reason") or ""))
        if agent not in by_agent:
            by_agent[agent] = {
                "agent_id": agent,
                "impressions": 0,
                "adoptions": 0,
                "exact_adopts": 0,
                "partial_adopts": 0,
                "edit_adopts": 0,
                "polish_runs": 0,
                "polished_adopts": 0,
            }
        ag = by_agent[agent]

        if action == "copilot_impression":
            ag["impressions"] += 1
            trig = str(meta.get("trigger") or "open")
            bt = by_trigger.setdefault(trig, {"impressions": 0, "adoptions": 0})
            bt["impressions"] += 1
        elif action == "copilot_adopt":
            ag["adoptions"] += 1
            match = str(meta.get("match") or "exact")
            if match == "exact":
                ag["exact_adopts"] += 1
            elif match == "partial":
                ag["partial_adopts"] += 1
            else:
                ag["edit_adopts"] += 1
            if meta.get("polished"):
                ag["polished_adopts"] += 1
            trig = str(meta.get("trigger") or "open")
            bt = by_trigger.setdefault(trig, {"impressions": 0, "adoptions": 0})
            bt["adoptions"] += 1
            src = str(meta.get("source") or "unknown")
            bs = by_source.setdefault(src, {"adoptions": 0})
            bs["adoptions"] += 1
            replays.append({
                "ts": float(row.get("ts") or 0),
                "agent_id": agent,
                "conversation_id": str(row.get("conversation_id") or ""),
                "match": match,
                "source": src,
                "polished": bool(meta.get("polished")),
                "trigger": trig,
                "stage": str(meta.get("stage") or ""),
                "suggested": str(meta.get("suggested") or ""),
                "sent": str(meta.get("sent") or ""),
            })
        elif action == "copilot_polish":
            ag["polish_runs"] += 1

    agents: List[Dict[str, Any]] = []
    total_imp = total_adopt = 0
    for ag in by_agent.values():
        imp = int(ag["impressions"] or 0)
        ad = int(ag["adoptions"] or 0)
        ag["adoption_rate"] = round(ad / imp * 100, 1) if imp else 0.0
        ag["strict_rate"] = round(
            (ag["exact_adopts"] + ag["partial_adopts"]) / imp * 100, 1,
        ) if imp else 0.0
        agents.append(ag)
        total_imp += imp
        total_adopt += ad

    agents.sort(key=lambda x: (-x.get("adoptions", 0), -x.get("impressions", 0)))
    replays.sort(key=lambda r: float(r.get("ts") or 0), reverse=True)

    return {
        "agents": agents,
        "total_impressions": total_imp,
        "total_adoptions": total_adopt,
        "overall_rate": round(total_adopt / total_imp * 100, 1) if total_imp else 0.0,
        "by_trigger": by_trigger,
        "by_source": by_source,
        "replays": replays[:50],
    }


def classify_adoption(suggested: str, sent: str, *, applied_ts: float = 0) -> str:
    """判定采纳类型：exact / partial / edit。"""
    s = (suggested or "").strip()
    t = (sent or "").strip()
    if not s or not t:
        return "edit"
    if s == t:
        return "exact"
    if t.startswith(s[: min(20, len(s))]) or s.startswith(t[: min(20, len(t))]):
        return "partial"
    return "edit"
