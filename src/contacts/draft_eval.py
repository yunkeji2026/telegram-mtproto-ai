"""W3-3G：reunion 草稿成功率评估器。

闭环逻辑：
  - ``record_draft`` → ``mark_draft_sent`` 之后，等 ``eval_window_secs`` 过去
  - 看该 ``journey_id`` 在 ``[sent_ts, sent_ts + eval_window_secs]`` 区间内有没有
    新的 ``msg_in``（对方主动回信）
  - 有 → success=True；没 → success=False
  - 写回 ``draft_log.success_eval_ts / success / reply_event_id``

设计要点：
  - **幂等**：``eval_draft_success`` 用 ``WHERE success_eval_ts IS NULL`` 守门
  - **窗口可调**：默认 24h，配置驱动
  - **不阻塞主流程**：失败 log warning，不抛
  - **只关心 sent_ts 之后的 msg_in**：避免把"草稿生成前对方早就回的消息"
    误算为 success
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class DraftSuccessEvaluator:
    """对已发送但未评估的 draft 做"24h 内是否被回复"判定。"""

    def __init__(
        self,
        store,
        *,
        eval_window_secs: int = 86400,
    ) -> None:
        self._store = store
        self._eval_window_secs = int(max(60, eval_window_secs))

    @property
    def eval_window_secs(self) -> int:
        return self._eval_window_secs

    def evaluate_due(
        self, *, now_ts: Optional[int] = None, limit: int = 500,
    ) -> Dict[str, int]:
        """对所有"已发 + 窗口过期 + 未评估"的 draft 跑评估。

        返回 ``{evaluated, success, fail}`` 计数。
        """
        pending = self._store.list_drafts_pending_eval(
            window_secs=self._eval_window_secs,
            now_ts=now_ts, limit=limit,
        )
        success_n = 0
        fail_n = 0
        for d in pending:
            sent_ts = int(d.get("sent_ts") or 0)
            if not sent_ts:
                continue
            deadline = sent_ts + self._eval_window_secs
            reply = self._store.find_first_msg_in_window(
                d["journey_id"], after_ts=sent_ts, before_ts=deadline,
            )
            ok = reply is not None
            try:
                self._store.eval_draft_success(
                    d["draft_id"], success=ok,
                    reply_event_id=(reply or {}).get("event_id", ""),
                    now_ts=now_ts,
                )
            except Exception as e:
                logger.warning("eval_draft_success failed for %s: %s",
                               d.get("draft_id"), e)
                continue
            if ok:
                success_n += 1
            else:
                fail_n += 1
        return {
            "evaluated": success_n + fail_n,
            "success": success_n,
            "fail": fail_n,
        }


class DraftEvalScheduler:
    """W3-3K.1：可观测的草稿评估调度器。

    包装 ``DraftSuccessEvaluator``，额外提供：
      - 运行状态（last_run_at / last_result / next_run_at）
      - **自适应间隔**：若本轮 evaluated==0（无待评估），下轮间隔翻倍（最大 2×base）；
        若本轮评估了内容，立刻重置为 base_interval_secs。
        原理：有活跃草稿发出时高频轮询（1h），静默期降到 2h，减少空转。
      - 线程安全（all state under ``_lock``）
      - ``run_once()`` 可供调度循环或手动触发共用（状态都更新）

    设计取舍：
      - 状态存内存不存 DB：last_run 通过日志可追溯；DB 持久化价值低于复杂度成本
      - ``run_once()`` 不抛异常：失败 log warning，返回空结果 dict
    """

    _BASE_INTERVAL_SECS = 3600      # 1h：正常情况
    _MAX_INTERVAL_SECS  = 7200      # 2h：静默期最长间隔
    _BACKOFF_THRESHOLD  = 0         # evaluated <= 此值时触发 back-off

    def __init__(
        self,
        store,
        *,
        eval_window_secs: int = 86400,
        base_interval_secs: int = _BASE_INTERVAL_SECS,
        max_interval_secs: int = _MAX_INTERVAL_SECS,
    ) -> None:
        self._evaluator = DraftSuccessEvaluator(
            store, eval_window_secs=eval_window_secs,
        )
        self._base_interval = int(max(60, base_interval_secs))
        self._max_interval  = int(max(self._base_interval, max_interval_secs))
        self._lock = threading.Lock()
        # observable state
        self._last_run_at:   Optional[float] = None
        self._last_result:   Optional[Dict[str, int]] = None
        self._next_run_at:   Optional[float] = None
        self._current_interval: int = self._base_interval
        self._total_runs:    int = 0
        self._is_running:    bool = False

    # ── public API ────────────────────────────────────────────────────────────

    def run_once(self, *, now_ts: Optional[int] = None) -> Dict[str, int]:
        """跑一轮评估，更新内部状态。线程安全，不抛。"""
        with self._lock:
            self._is_running = True
        result: Dict[str, int] = {"evaluated": 0, "success": 0, "fail": 0}
        try:
            result = self._evaluator.evaluate_due(now_ts=now_ts)
        except Exception as e:
            logger.warning("DraftEvalScheduler.run_once error: %s", e)
        finally:
            now = time.time()
            with self._lock:
                self._is_running = False
                self._last_run_at = now
                self._last_result = result
                self._total_runs += 1
                # 自适应间隔
                if result.get("evaluated", 0) <= self._BACKOFF_THRESHOLD:
                    self._current_interval = min(
                        self._current_interval * 2, self._max_interval,
                    )
                else:
                    self._current_interval = self._base_interval
                self._next_run_at = now + self._current_interval
                if result.get("evaluated", 0):
                    logger.info(
                        "draft eval: evaluated=%d success=%d fail=%d "
                        "next_in=%ds",
                        result["evaluated"], result["success"], result["fail"],
                        self._current_interval,
                    )
        return result

    @property
    def next_interval_secs(self) -> int:
        with self._lock:
            return self._current_interval

    def status(self) -> Dict[str, Any]:
        """返回调度器当前可观测状态（API / UI 用）。"""
        with self._lock:
            last_run  = self._last_run_at
            last_res  = dict(self._last_result) if self._last_result else None
            next_run  = self._next_run_at
            total     = self._total_runs
            running   = self._is_running
            interval  = self._current_interval
        now = time.time()
        return {
            "last_run_at": int(last_run) if last_run else None,
            "last_run_ago_secs": int(now - last_run) if last_run else None,
            "last_result": last_res,
            "next_run_at": int(next_run) if next_run else None,
            "next_run_in_secs": max(0, int(next_run - now)) if next_run else None,
            "current_interval_secs": interval,
            "base_interval_secs": self._base_interval,
            "total_runs": total,
            "is_running": running,
            "eval_window_secs": self._evaluator.eval_window_secs,
        }
