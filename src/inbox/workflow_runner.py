"""P44/P47 — 陪伴剧本自动执行链（WorkflowRunner）。

在 ScheduledReporter 每 tick 调用，处理到期的 workflow_executions：
  - 按 steps_json 顺序执行各步骤
  - 支持 delay_hours 延迟（下一步写入 next_step_at）
  - template 步骤 → 发布 workflow_step 事件（坐席通知 + 建议话术）
  - task / note / tag 步骤 → 直接执行（自动化）
  - P47：max_steps_per_tick 防阻塞；步骤失败自动重试 1 次
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_STEPS_PER_TICK = 50
STEP_RETRY_DELAY_SEC = 30
MAX_STEP_RETRIES = 1


class WorkflowRunner:
    """P44：工作链步骤执行器。"""

    def __init__(self, inbox_store: Any, contacts_store: Any = None) -> None:
        self._store = inbox_store
        self._contacts = contacts_store

    # ── 公开接口 ─────────────────────────────────────────────────────────────

    def process_due_executions(
        self, *, now: Optional[float] = None, max_steps: int = MAX_STEPS_PER_TICK,
    ) -> int:
        """处理所有到期的工作链执行，返回处理条数。"""
        now = now or time.time()
        due = self._store.list_due_workflow_executions(now)
        processed = 0
        budget = max(1, int(max_steps))
        for ex in due:
            if budget <= 0:
                break
            try:
                n = self._drain_execution(ex["exec_id"], now, max_steps=budget)
                budget -= n
                if n > 0:
                    processed += 1
            except Exception:
                logger.debug("WorkflowRunner 单条执行失败", exc_info=True)
        return processed

    def auto_start_chains(self, *, now: Optional[float] = None) -> int:
        """P44+P35：根据链 trigger_conditions 自动启动（沉默/流失）。"""
        now = now or time.time()
        chains = self._store.list_workflow_chains()
        started = 0
        for chain in chains:
            if not chain.get("enabled"):
                continue
            try:
                conds = json.loads(chain.get("trigger_conditions") or "{}")
            except Exception:
                conds = {}
            if not conds:
                continue
            silence_days = float(conds.get("silence_days") or 0)
            churn_only = bool(conds.get("churn_risk_high"))
            if silence_days <= 0 and not churn_only:
                continue
            candidates = self._find_chain_candidates(silence_days, churn_only, now)
            for cid in candidates:
                if self._store.has_running_chain(cid, chain["chain_id"]):
                    continue
                self._store.start_chain_execution(
                    chain["chain_id"], cid,
                    {"auto": True, "trigger": conds},
                    schedule_first_step=True,
                )
                started += 1
        return started

    # ── 单条执行推进 ─────────────────────────────────────────────────────────

    def _drain_execution(self, exec_id: str, now: float, *, max_steps: int) -> int:
        """对单条执行记录连续推进至多 max_steps 步（零延迟链同 tick 跑完）。"""
        steps_done = 0
        while steps_done < max_steps:
            ex = self._store.get_workflow_execution(exec_id)
            if not ex or ex.get("status") != "running":
                break
            cont = self._advance_one_step(ex, now)
            steps_done += 1
            if not cont:
                break
        return steps_done

    def _advance_one_step(self, ex: Dict[str, Any], now: float) -> bool:
        """执行一步。返回 True 表示可立即继续下一步（零延迟），False 表示等待或结束。"""
        exec_id = ex["exec_id"]
        chain_id = ex["chain_id"]
        conv_id = ex["conversation_id"]
        step_idx = int(ex.get("current_step") or 0)

        chain = self._store.get_workflow_chain(chain_id)
        if not chain:
            self._fail_execution(ex, conv_id, "chain_not_found")
            return False

        try:
            steps = json.loads(chain.get("steps_json") or "[]")
        except Exception:
            steps = []

        if step_idx >= len(steps):
            self._store.complete_workflow_execution(exec_id, status="completed")
            return False

        step = steps[step_idx]
        result = self._execute_step(conv_id, step, ex)

        if not result.get("ok", True):
            if self._schedule_step_retry(ex, step_idx, result, now):
                return False
            self._fail_execution(ex, conv_id, "step_failed", result=result)
            return False

        next_idx = step_idx + 1
        if next_idx >= len(steps):
            self._store.complete_workflow_execution(exec_id, status="completed")
            self._publish_status_event(conv_id, ex, "completed")
            logger.info("WorkflowRunner 链完成: %s conv=%s", chain_id, conv_id)
            return False

        next_step = steps[next_idx]
        delay_h = float(next_step.get("delay_hours") or 0)
        ctx = self._load_context(ex)
        retries = ctx.get("step_retries") or {}
        retries.pop(str(step_idx), None)
        ctx["step_retries"] = retries

        if delay_h > 0:
            next_at = now + delay_h * 3600
            self._store.update_workflow_execution(
                exec_id,
                current_step=next_idx,
                next_step_at=next_at,
                last_result=result,
                context_json=ctx,
            )
            return False

        self._store.update_workflow_execution(
            exec_id,
            current_step=next_idx,
            next_step_at=0,
            last_result=result,
            context_json=ctx,
        )
        return True

    def _schedule_step_retry(
        self, ex: Dict[str, Any], step_idx: int, result: Dict[str, Any], now: float,
    ) -> bool:
        """步骤失败时调度重试，返回 True 表示已调度。"""
        ctx = self._load_context(ex)
        retries = ctx.get("step_retries") or {}
        key = str(step_idx)
        count = int(retries.get(key, 0))
        if count >= MAX_STEP_RETRIES:
            return False
        retries[key] = count + 1
        ctx["step_retries"] = retries
        self._store.update_workflow_execution(
            ex["exec_id"],
            current_step=step_idx,
            next_step_at=now + STEP_RETRY_DELAY_SEC,
            last_result={**result, "retry": count + 1},
            context_json=ctx,
        )
        logger.info(
            "WorkflowRunner 步骤重试: exec=%s step=%s retry=%s",
            ex["exec_id"], step_idx, count + 1,
        )
        return True

    def _fail_execution(
        self,
        ex: Dict[str, Any],
        conv_id: str,
        reason: str,
        *,
        result: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._store.complete_workflow_execution(ex["exec_id"], status="failed")
        self._publish_status_event(conv_id, ex, "failed", reason=reason, result=result)

    def _load_context(self, ex: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return json.loads(ex.get("context_json") or "{}")
        except Exception:
            return {}

    def _execute_step(
        self, conv_id: str, step: Dict[str, Any], ex: Dict[str, Any],
    ) -> Dict[str, Any]:
        action_type = str(step.get("action_type") or "template")
        note = str(step.get("note") or step.get("text") or "")
        result: Dict[str, Any] = {"action_type": action_type, "ok": True}

        if action_type == "template":
            self._publish_step_event(conv_id, ex, note, action_type)
            result["text"] = note

        elif action_type == "note" and note:
            try:
                self._store.add_conv_note(
                    conv_id, note,
                    agent_id="system", agent_name="工作链",
                )
                result["note_added"] = True
            except Exception:
                result["ok"] = False
            self._publish_step_event(conv_id, ex, f"📝 已添加内部备注：{note[:60]}", "note")

        elif action_type == "tag":
            tag = str(step.get("tag") or note or "").strip()
            if tag:
                try:
                    existing = self._store.get_conv_tags(conv_id)
                    if tag not in existing:
                        self._store.set_conv_tags(conv_id, existing + [tag])
                    result["tag"] = tag
                except Exception:
                    result["ok"] = False

        elif action_type == "task":
            due_h = float(step.get("delay_hours") or 72)
            if self._contacts:
                try:
                    meta = self._store.get_conv_meta(conv_id) or {}
                    contact_id = str(meta.get("contact_id") or "")
                    if contact_id:
                        self._contacts.add_follow_up_task(
                            contact_id, time.time() + due_h * 3600, note=note,
                        )
                        result["task_created"] = True
                    else:
                        result["ok"] = False
                except Exception:
                    result["ok"] = False
            else:
                result["ok"] = False

        else:
            self._publish_step_event(conv_id, ex, note, action_type)

        return result

    def _publish_step_event(
        self, conv_id: str, ex: Dict[str, Any], text: str, action_type: str,
    ) -> None:
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("workflow_step", {
                "conversation_id": conv_id,
                "exec_id": ex.get("exec_id"),
                "chain_id": ex.get("chain_id"),
                "chain_name": ex.get("chain_name", ""),
                "action_type": action_type,
                "suggested_text": text,
                "step": int(ex.get("current_step") or 0),
                "ts": time.time(),
            })
        except Exception:
            logger.debug("workflow_step 事件发布失败", exc_info=True)

    def _publish_status_event(
        self,
        conv_id: str,
        ex: Dict[str, Any],
        status: str,
        *,
        reason: str = "",
        result: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("workflow_execution_" + status, {
                "conversation_id": conv_id,
                "exec_id": ex.get("exec_id"),
                "chain_id": ex.get("chain_id"),
                "chain_name": ex.get("chain_name", ""),
                "status": status,
                "reason": reason,
                "result": result or {},
                "step": int(ex.get("current_step") or 0),
                "ts": time.time(),
            })
        except Exception:
            logger.debug("workflow_execution 状态事件发布失败", exc_info=True)

    def _find_chain_candidates(
        self, silence_days: float, churn_only: bool, now: float,
    ) -> List[str]:
        """查找符合自动触发条件的会话 ID。"""
        cutoff = now - silence_days * 86400
        convs = self._store.list_churn_risk_conversations(
            silence_days=max(1, int(silence_days)), limit=50,
        )
        cids: List[str] = []
        for c in convs:
            cid = str(c.get("conversation_id") or "")
            if not cid:
                continue
            if float(c.get("last_ts") or 0) > cutoff and silence_days > 0:
                continue
            if churn_only:
                churn_raw = str(c.get("churn_risk") or "")
                if churn_raw:
                    try:
                        cd = json.loads(churn_raw)
                        if cd.get("level") != "high":
                            continue
                    except Exception:
                        continue
                else:
                    continue
            cids.append(cid)
        return cids[:20]
