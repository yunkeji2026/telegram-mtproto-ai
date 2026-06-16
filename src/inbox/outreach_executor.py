"""P61-4：分组批量触达 **执行 + 回执闭环**（真实发送）。

在 P61-3 的 dry-run 规划层之上接执行：消费 `OutreachPlan.eligible`，逐个
**真实扣减日配额**（`AccountLimiter.check_and_reserve`）→ 渲染模板 → 经注入的
`send_fn` 投递 → `record_outreach` 落回执，最后汇总 batch 统计。

安全设计：
- `send_fn` 可注入（异步 `(target, text) -> result`），单测无需真实 RPA。
- **只对真实发送尝试（sent/failed）写 outreach_log**——配额拒绝/空模板不写，
  保持 cooldown 语义干净（仅"确实触达过"才计入冷却）。
- 配额拒绝时**不退还**（与 AccountLimiter 约定一致：占坑即不退，防风控聚合）。
- pacing：发送间隔可配（`sleep_fn` 注入，便于测试零等待）。
- 永不让单条异常中断整批：逐条 try，失败记 failed 继续。
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional

from src.inbox.outreach_planner import OutreachTarget

logger = logging.getLogger(__name__)

# send_fn: async (target, text) -> 任意结果（抛异常视为失败）
SendFn = Callable[[OutreachTarget, str], Awaitable[Any]]
SleepFn = Callable[[float], Awaitable[None]]


def render_template(template: str, target: OutreachTarget) -> str:
    """渲染触达模板，支持占位符 {name}/{silent_days}/{platform}。

    {name} 缺省回落「朋友」，避免出现空昵称的尴尬开场。
    """
    name = (target.display_name or "").strip() or "朋友"
    out = str(template or "")
    repl = {
        "{name}": name,
        "{silent_days}": str(int(target.silent_days or 0)),
        "{platform}": target.platform or "",
    }
    for k, v in repl.items():
        out = out.replace(k, v)
    return out


class OutreachExecutor:
    def __init__(
        self,
        store,
        send_fn: SendFn,
        limiter=None,
        *,
        per_send_seconds: float = 0.0,
        sleep_fn: Optional[SleepFn] = None,
    ) -> None:
        self._store = store
        self._send_fn = send_fn
        self._limiter = limiter
        self._per_send = max(0.0, float(per_send_seconds))
        self._sleep_fn = sleep_fn

    async def execute(
        self,
        targets: List[OutreachTarget],
        template: str,
        *,
        batch_id: str = "",
        max_send: int = 0,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        batch_id = str(batch_id or "").strip() or uuid.uuid4().hex[:12]
        now = int(now if now is not None else time.time())
        sent = failed = skipped = 0
        details: List[Dict[str, Any]] = []
        attempted = 0

        for t in targets:
            if max_send and attempted >= max_send:
                break
            text = render_template(template, t)
            if not text.strip():
                skipped += 1
                details.append({"conversation_id": t.conversation_id,
                                "status": "skipped", "reason": "empty_text"})
                continue
            # 真实配额扣减（拒绝不写 log、不退还）
            if self._limiter is not None:
                dec = self._limiter.check_and_reserve(t.account_id, now=now)
                if not getattr(dec, "ok", False):
                    skipped += 1
                    details.append({"conversation_id": t.conversation_id,
                                    "status": "skipped",
                                    "reason": getattr(dec, "reason", "cap")})
                    continue
            attempted += 1
            try:
                await self._send_fn(t, text)
                self._store.record_outreach(
                    t.conversation_id, batch_id=batch_id, platform=t.platform,
                    account_id=t.account_id, status="sent", ts=now,
                )
                sent += 1
                details.append({"conversation_id": t.conversation_id, "status": "sent"})
            except Exception as exc:  # noqa: BLE001
                self._store.record_outreach(
                    t.conversation_id, batch_id=batch_id, platform=t.platform,
                    account_id=t.account_id, status="failed",
                    note=str(exc)[:200], ts=now,
                )
                failed += 1
                details.append({"conversation_id": t.conversation_id,
                                "status": "failed", "error": str(exc)[:120]})
                logger.warning("outreach 发送失败 conv=%s: %s", t.conversation_id, exc)
            # pacing（最后一条后不必等；但实现简单起见统一 sleep，由 sleep_fn 决定）
            if self._per_send > 0 and self._sleep_fn is not None:
                try:
                    await self._sleep_fn(self._per_send)
                except Exception:
                    pass

        return {
            "ok": True,
            "batch_id": batch_id,
            "total": attempted + skipped,
            "attempted": attempted,
            "sent": sent,
            "failed": failed,
            "skipped": skipped,
            "details": details,
        }


__all__ = ["OutreachExecutor", "render_template", "SendFn"]
