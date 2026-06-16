"""P48 — 协作 @mention 智能路由。

按关系阶段、QA 表现、在线状态、负荷与阶段确认历史，为注解 @ 推荐坐席。
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Set

from src.utils.companion_relationship import STAGE_LABEL_ZH, STAGE_ORDER

_SUPERVISOR_ROLES = frozenset({"master", "admin"})
_HIGH_CHURN_STAGES = frozenset({"intimate", "steady"})


class MentionRouter:
    """P48：@mention 坐席推荐引擎。"""

    def __init__(
        self,
        inbox_store: Any,
        *,
        presence: Optional[List[Dict[str, Any]]] = None,
        workloads: Optional[List[Dict[str, Any]]] = None,
        qa_stats: Optional[List[Dict[str, Any]]] = None,
        users: Optional[List[Dict[str, Any]]] = None,
        stage_confirm: Optional[Dict[str, Dict[str, int]]] = None,
        mention_counts: Optional[Dict[str, int]] = None,
    ) -> None:
        self._store = inbox_store
        self._presence = presence or []
        self._workloads = workloads or []
        self._qa_stats = qa_stats or []
        self._users = users or []
        self._stage_confirm = stage_confirm or {}
        self._mention_counts = mention_counts or {}

    @classmethod
    def from_store(
        cls,
        store: Any,
        *,
        presence: Optional[List[Dict[str, Any]]] = None,
        users: Optional[List[Dict[str, Any]]] = None,
        days: int = 30,
    ) -> "MentionRouter":
        since = time.time() - max(1, int(days)) * 86400
        workloads = store.list_agent_workloads() if store else []
        qa_stats = store.batch_agent_qa_stats(days=days) if store else []
        stage_confirm = store.get_agent_stage_confirm_counts(since_ts=since) if store else {}
        mention_counts = store.get_agent_mention_counts(since_ts=since) if store else {}
        return cls(
            store,
            presence=presence,
            workloads=workloads,
            qa_stats=qa_stats,
            users=users,
            stage_confirm=stage_confirm,
            mention_counts=mention_counts,
        )

    def suggest(
        self,
        *,
        stage: str = "initial",
        stage_label: str = "",
        churn_level: str = "",
        claim_agent_id: str = "",
        overdue_chain: bool = False,
        exclude_agent_id: str = "",
        query: str = "",
        limit: int = 8,
    ) -> Dict[str, Any]:
        """返回排序后的坐席推荐列表 + 可选自动抄送主管。"""
        stage = str(stage or "initial").strip()
        stage_label = stage_label or STAGE_LABEL_ZH.get(stage, stage)
        q = str(query or "").strip().lower()
        limit = max(1, min(20, int(limit or 8)))

        candidates = self._build_candidates()
        if not candidates:
            return {
                "suggestions": [],
                "auto_cc": [],
                "stage": stage,
                "stage_label": stage_label,
            }

        max_load = max((c.get("active_convs") or 0) for c in candidates) or 1
        scored: List[Dict[str, Any]] = []
        for c in candidates:
            aid = c["agent_id"]
            if exclude_agent_id and aid == exclude_agent_id:
                continue
            if q and q not in aid.lower() and q not in str(c.get("display_name") or "").lower():
                continue
            reasons: List[str] = []
            score = 0.0

            status = str(c.get("status") or "offline")
            if status == "online":
                score += 30
                reasons.append("在线")
            elif status == "busy":
                score += 18
                reasons.append("忙碌可协助")

            load = int(c.get("active_convs") or 0)
            load_bonus = 20 * (1 - min(1.0, load / max(max_load, 1)))
            score += load_bonus
            if load <= 1:
                reasons.append("负荷较轻")

            qa = c.get("avg_score")
            if qa is not None:
                score += min(20, float(qa) / 5)
                if qa >= 80:
                    reasons.append(f"QA 均分 {int(qa)}")

            stage_hits = int((self._stage_confirm.get(aid) or {}).get(stage) or 0)
            if stage_hits > 0:
                score += min(25, 10 + stage_hits * 3)
                reasons.append(f"擅长「{stage_label}」({stage_hits}次确认)")

            mcnt = int(self._mention_counts.get(aid) or 0)
            if mcnt >= 3:
                score += min(10, mcnt)
                reasons.append("协作活跃")

            if claim_agent_id and aid == claim_agent_id:
                score += 12
                reasons.append("当前认领坐席")

            if str(c.get("role") or "") in _SUPERVISOR_ROLES:
                score += 5
                if churn_level == "high" and stage in _HIGH_CHURN_STAGES:
                    score += 15
                    reasons.append("主管 · 高流失客户")

            if overdue_chain and claim_agent_id and aid == claim_agent_id:
                score += 20
                reasons.append("工作链待处理")

            scored.append({
                "agent_id": aid,
                "display_name": c.get("display_name") or aid,
                "role": c.get("role") or "",
                "status": status,
                "active_convs": load,
                "avg_qa_score": qa,
                "score": round(score, 1),
                "reasons": reasons[:4],
                "is_supervisor": str(c.get("role") or "") in _SUPERVISOR_ROLES,
            })

        scored.sort(key=lambda x: (-x["score"], x["active_convs"], x["agent_id"]))
        suggestions = scored[:limit]

        auto_cc: List[Dict[str, Any]] = []
        if churn_level == "high" and stage in _HIGH_CHURN_STAGES:
            sup = next((s for s in scored if s["is_supervisor"]), None)
            if sup:
                auto_cc.append({
                    "agent_id": sup["agent_id"],
                    "display_name": sup["display_name"],
                    "reason": f"高流失 + {stage_label} 阶段建议抄送主管",
                })

        return {
            "suggestions": suggestions,
            "auto_cc": auto_cc,
            "stage": stage,
            "stage_label": stage_label,
            "churn_level": churn_level,
            "overdue_chain": overdue_chain,
        }

    def _build_candidates(self) -> List[Dict[str, Any]]:
        by_id: Dict[str, Dict[str, Any]] = {}

        user_roles = {
            str(u.get("username") or ""): {
                "role": str(u.get("role") or ""),
                "display_name": str(u.get("display_name") or u.get("username") or ""),
            }
            for u in self._users
            if u.get("enabled", 1)
        }

        for p in self._presence:
            aid = str(p.get("agent_id") or "").strip()
            if not aid:
                continue
            ur = user_roles.get(aid, {})
            by_id[aid] = {
                "agent_id": aid,
                "display_name": p.get("display_name") or ur.get("display_name") or aid,
                "status": p.get("status") or "offline",
                "role": ur.get("role") or "",
                "active_convs": 0,
                "avg_score": None,
            }

        for wl in self._workloads:
            aid = str(wl.get("agent_id") or "").strip()
            if not aid:
                continue
            ur = user_roles.get(aid, {})
            row = by_id.setdefault(aid, {
                "agent_id": aid,
                "display_name": ur.get("display_name") or aid,
                "status": wl.get("status") or "offline",
                "role": ur.get("role") or "",
                "active_convs": 0,
                "avg_score": None,
            })
            row["active_convs"] = int(wl.get("active_convs") or 0)
            row["status"] = wl.get("status") or row["status"]

        qa_map = {str(q.get("agent_id") or ""): q for q in self._qa_stats}
        for aid, row in by_id.items():
            qa = qa_map.get(aid)
            if qa:
                row["avg_score"] = qa.get("avg_score")
                if not row.get("display_name") or row["display_name"] == aid:
                    row["display_name"] = qa.get("agent_name") or aid

        # 补充仅有 QA/历史但当前离线的坐席
        for aid, qa in qa_map.items():
            if aid and aid not in by_id:
                ur = user_roles.get(aid, {})
                by_id[aid] = {
                    "agent_id": aid,
                    "display_name": ur.get("display_name") or qa.get("agent_name") or aid,
                    "status": "offline",
                    "role": ur.get("role") or "",
                    "active_convs": 0,
                    "avg_score": qa.get("avg_score"),
                }

        return list(by_id.values())
