"""P47 — 工作链执行可视化：记录富化与监控辅助。"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

_TYPE_LABELS = {
    "template": "话术模板",
    "task": "创建任务",
    "tag": "添加标签",
    "note": "内部备注",
    "escalate": "升级人工",
    "chain": "触发工作链",
}

_STATUS_LABELS = {
    "running": "运行中",
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
    "pending": "待执行",
}


def enrich_execution(row: Dict[str, Any], *, now: Optional[float] = None) -> Dict[str, Any]:
    """将 workflow_executions 行富化为前端可展示结构。"""
    ts = float(now if now is not None else time.time())
    try:
        steps = json.loads(row.get("steps_json") or "[]")
    except Exception:
        steps = []
    try:
        last_result = json.loads(row.get("last_result_json") or "{}")
    except Exception:
        last_result = {}
    try:
        context = json.loads(row.get("context_json") or "{}")
    except Exception:
        context = {}

    total = len(steps)
    cur = int(row.get("current_step") or 0)
    status = str(row.get("status") or "pending")
    next_at = float(row.get("next_step_at") or 0)

    cur_step = steps[cur] if 0 <= cur < total else None
    preview: List[Dict[str, Any]] = []
    for i, s in enumerate(steps[:8]):
        preview.append({
            "index": i,
            "done": i < cur or status in ("completed", "cancelled"),
            "active": i == cur and status == "running",
            "action_type": s.get("action_type", "template"),
            "action_label": _TYPE_LABELS.get(s.get("action_type", ""), s.get("action_type", "")),
            "note": str(s.get("note") or s.get("text") or "")[:80],
            "delay_hours": float(s.get("delay_hours") or 0),
        })

    countdown = max(0, int(next_at - ts)) if next_at > ts else 0
    return {
        "exec_id": row.get("exec_id"),
        "chain_id": row.get("chain_id"),
        "chain_name": row.get("chain_name") or row.get("chain_id") or "",
        "conversation_id": row.get("conversation_id"),
        "display_name": row.get("display_name") or "",
        "platform": row.get("platform") or "",
        "status": status,
        "status_label": _STATUS_LABELS.get(status, status),
        "current_step": cur,
        "current_step_display": min(cur + 1, total) if total else 0,
        "total_steps": total,
        "current_step_type": (cur_step or {}).get("action_type", ""),
        "current_step_label": _TYPE_LABELS.get(
            (cur_step or {}).get("action_type", ""), (cur_step or {}).get("action_type", ""),
        ),
        "current_step_note": str((cur_step or {}).get("note") or (cur_step or {}).get("text") or "")[:120],
        "steps_preview": preview,
        "last_result": last_result,
        "context": context,
        "started_at": float(row.get("started_at") or 0),
        "updated_at": float(row.get("updated_at") or 0),
        "next_step_at": next_at,
        "countdown_sec": countdown,
        "is_due": status == "running" and next_at > 0 and next_at <= ts,
        "progress_pct": round(cur / total * 100) if total else 0,
    }


def enrich_executions(rows: List[Dict[str, Any]], *, now: Optional[float] = None) -> List[Dict[str, Any]]:
    return [enrich_execution(r, now=now) for r in rows]
