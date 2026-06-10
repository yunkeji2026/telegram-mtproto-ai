"""P61-3：合规分组批量触达（再激活）——**纯 dry-run 规划层**。

定位「再激活」而非「群发」：按标签 / 关系阶段 / 沉默天数圈选统一收件箱里的会话，
再叠加 **账号级日配额 + cooldown** 算出"今天实际能触达谁"，产出可预览的计划。

设计纪律（与 P61-1 一致：先安全网后执行）：
- 本模块**只读不发**：不调用任何 RPA、不写 outreach_log、不动 AccountLimiter 计数。
  配额用 `limiter.remaining_for()`（只读）读取后在内存里模拟扣减。
- 实际发送 + 回执落库由后续 execution 阶段（P61-3-2）消费本计划。
- 纯函数式、可单测、不起 FastAPI app。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 每条触达的预计耗时（秒）——含 RPA pacing，用于估算批次总时长
_DEFAULT_PER_SEND_SECONDS = 8.0


@dataclass
class OutreachFilters:
    platform: str = ""               # 限定平台；空=全平台
    tags_any: List[str] = field(default_factory=list)   # 命中任一标签
    rel_stages: List[str] = field(default_factory=list)  # 关系阶段白名单
    min_silent_days: float = 0.0     # 至少沉默这么久（last_ts 早于 now-阈值）
    max_silent_days: float = 0.0     # 0=无上限；用于排除"已流失太久"
    exclude_archived: bool = True
    limit: int = 500                 # 扫描会话上限


@dataclass
class OutreachTarget:
    conversation_id: str
    platform: str
    account_id: str
    chat_key: str
    display_name: str
    last_ts: float
    silent_days: float
    tags: List[str]
    rel_stage: str


@dataclass
class OutreachPlan:
    generated_at: int
    total_matched: int
    eligible: List[OutreachTarget]
    skipped: List[Dict[str, Any]]            # [{conversation_id, reason}]
    per_account: Dict[str, Dict[str, int]]   # account_id -> {assigned, remaining_before, cap}
    estimated_seconds: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "total_matched": self.total_matched,
            "eligible_count": len(self.eligible),
            "skipped_count": len(self.skipped),
            "eligible": [t.__dict__ for t in self.eligible],
            "skipped": self.skipped,
            "per_account": self.per_account,
            "estimated_seconds": round(self.estimated_seconds, 1),
        }


class OutreachPlanner:
    def __init__(
        self,
        store,
        limiter=None,
        *,
        cooldown_days: float = 14.0,
        per_send_seconds: float = _DEFAULT_PER_SEND_SECONDS,
        default_account_cap: int = 0,   # 无 limiter 时的兜底每账号上限；0=不限
    ) -> None:
        self._store = store
        self._limiter = limiter
        self._cooldown_s = max(0.0, float(cooldown_days) * 86400.0)
        self._per_send = max(0.0, float(per_send_seconds))
        self._default_cap = max(0, int(default_account_cap))

    # ── 圈选 ──────────────────────────────────────────────
    def select_segment(
        self, filters: OutreachFilters, *, now: Optional[float] = None,
    ) -> List[OutreachTarget]:
        now = float(now if now is not None else time.time())
        rows = self._store.list_conversations(
            limit=max(1, int(filters.limit or 500)),
            platform=filters.platform or "",
        )
        min_cut = now - filters.min_silent_days * 86400.0 if filters.min_silent_days > 0 else None
        max_cut = now - filters.max_silent_days * 86400.0 if filters.max_silent_days > 0 else None
        tags_any = {str(t).strip() for t in (filters.tags_any or []) if str(t).strip()}
        rel_set = {str(s).strip() for s in (filters.rel_stages or []) if str(s).strip()}

        out: List[OutreachTarget] = []
        for r in rows:
            cid = str(r.get("conversation_id") or "")
            last_ts = float(r.get("last_ts") or 0)
            if min_cut is not None and last_ts > min_cut:
                continue   # 还不够沉默
            if max_cut is not None and last_ts < max_cut:
                continue   # 沉默太久（已流失），排除
            meta = self._meta(cid)
            if filters.exclude_archived and int(meta.get("archived") or 0):
                continue
            tags = meta.get("tags") or []
            if tags_any and not (tags_any & set(tags)):
                continue
            rel_stage = str(meta.get("rel_stage") or "")
            if rel_set and rel_stage not in rel_set:
                continue
            out.append(OutreachTarget(
                conversation_id=cid,
                platform=str(r.get("platform") or ""),
                account_id=str(r.get("account_id") or "default"),
                chat_key=str(r.get("chat_key") or ""),
                display_name=str(r.get("display_name") or ""),
                last_ts=last_ts,
                silent_days=round((now - last_ts) / 86400.0, 2) if last_ts else 0.0,
                tags=list(tags),
                rel_stage=rel_stage,
            ))
        # 最沉默的优先触达
        out.sort(key=lambda t: t.last_ts)
        return out

    # ── 计划（dry-run）────────────────────────────────────
    def build_plan(
        self, filters: OutreachFilters, *, now: Optional[float] = None,
    ) -> OutreachPlan:
        now = float(now if now is not None else time.time())
        targets = self.select_segment(filters, now=now)
        total = len(targets)

        # cooldown：批量取最近触达 ts
        last_map = {}
        if self._cooldown_s > 0 and targets:
            try:
                last_map = self._store.last_outreach_ts_bulk(
                    [t.conversation_id for t in targets]
                )
            except Exception:
                last_map = {}
        cooldown_cut = now - self._cooldown_s

        eligible: List[OutreachTarget] = []
        skipped: List[Dict[str, Any]] = []
        # 每账号剩余配额（只读快照 + 内存模拟扣减）
        remaining: Dict[str, int] = {}
        per_account: Dict[str, Dict[str, int]] = {}

        for t in targets:
            # cooldown
            if self._cooldown_s > 0:
                lt = float(last_map.get(t.conversation_id) or 0)
                if lt and lt >= cooldown_cut:
                    skipped.append({"conversation_id": t.conversation_id, "reason": "cooldown"})
                    continue
            acc = t.account_id
            if acc not in remaining:
                remaining[acc] = self._account_cap(acc, now=now)
                per_account[acc] = {
                    "assigned": 0,
                    "remaining_before": remaining[acc],
                    "cap": remaining[acc],
                }
            if remaining[acc] <= 0:
                skipped.append({"conversation_id": t.conversation_id, "reason": "account_cap"})
                continue
            remaining[acc] -= 1
            per_account[acc]["assigned"] += 1
            eligible.append(t)

        return OutreachPlan(
            generated_at=int(now),
            total_matched=total,
            eligible=eligible,
            skipped=skipped,
            per_account=per_account,
            estimated_seconds=len(eligible) * self._per_send,
        )

    # ── 内部 ──────────────────────────────────────────────
    def _account_cap(self, account_id: str, *, now: float) -> int:
        """账号今日剩余可触达额度。优先用 AccountLimiter（只读），否则兜底默认上限。"""
        if self._limiter is not None:
            try:
                return int(self._limiter.remaining_for(account_id, now=int(now)))
            except Exception:
                pass
        return self._default_cap if self._default_cap > 0 else 10 ** 9

    def _meta(self, conversation_id: str) -> Dict[str, Any]:
        """取会话 meta：tags(list) / archived / rel_stage。容错：无 meta 返回空壳。"""
        try:
            m = self._store.get_conv_meta(conversation_id) or {}
        except Exception:
            m = {}
        tags = m.get("conv_tags")
        if isinstance(tags, str):
            try:
                tags = json.loads(tags or "[]")
            except Exception:
                tags = []
        if not isinstance(tags, list):
            tags = []
        return {
            "tags": [str(x) for x in tags],
            "archived": int(m.get("archived") or 0),
            "rel_stage": str(m.get("rel_stage_cached") or ""),
        }


__all__ = ["OutreachPlanner", "OutreachFilters", "OutreachTarget", "OutreachPlan"]
