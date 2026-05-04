"""Vision 调用指标——SQLite-backed，跨重启可查。

**为什么需要**：

P0/P1/P2/P3 加完 vision 兜底后，当前生产环境无法回答这些问题：
  - input_verify p50/p95 延迟现在是多少？
  - 哪个任务最贵（按 token / 按时间）？
  - flash → plus 切换后实际 ok_rate 变化多少？
  - 哪些 task_name 的 error 集中在哪一类？

P5 沉淀了"任务-模型"经验进显式表，但下次想动这个表（比如试 flash 的
新版本），靠的还是"实测一两次的样本"。**P6 收集长期分布数据**，让后续
优化决策从猜测变为数据驱动。

数据规模：每条 send 1-3 次 vision 调用 × 100 send/day = 100-300 行/day。
一年 ~10w 行——SQLite 完全 OK，无需 retention。

设计选择：
  - 不写 metrics_store（内存式，重启丢；且按业务字段展开，按 task 分桶不
    适配）
  - 不写各 messenger_rpa_state_{account}.db（vision 是全局共享的，按账号
    分割反而难查）
  - 独立 ``config/vision_metrics.db`` 单表，最简单
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)


_DEFAULT_DB_PATH = Path(__file__).resolve().parents[3] / "config" / "vision_metrics.db"

# 进程内锁——sqlite 自身在多线程下需要明确的连接管理
_lock = threading.Lock()
_initialized = False
_db_path: Path = _DEFAULT_DB_PATH

_DDL = """
CREATE TABLE IF NOT EXISTS vision_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    task_name TEXT NOT NULL,
    model TEXT,
    api_provider TEXT,
    duration_ms INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    error_class TEXT
);
CREATE INDEX IF NOT EXISTS ix_vision_calls_ts ON vision_calls(ts);
CREATE INDEX IF NOT EXISTS ix_vision_calls_task_ts ON vision_calls(task_name, ts);
"""


def configure(db_path: Path | str | None = None) -> None:
    """覆盖默认 db 路径（测试用）。生产中保持默认。"""
    global _db_path, _initialized
    with _lock:
        if db_path is not None:
            _db_path = Path(db_path)
        _initialized = False


def _ensure_init() -> None:
    global _initialized
    if _initialized:
        return
    with _lock:
        if _initialized:
            return
        try:
            _db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(str(_db_path)) as conn:
                conn.executescript(_DDL)
            _initialized = True
        except Exception as e:
            logger.warning(
                "[vision_metrics] 初始化 db 失败 path=%s err=%s",
                _db_path, e,
            )
            # 不抛——metrics 是 best-effort，不能拖垮主流程


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    _ensure_init()
    c = sqlite3.connect(str(_db_path), timeout=2.0)
    try:
        yield c
        c.commit()
    finally:
        c.close()


def record(
    *,
    task_name: str,
    model: Optional[str],
    api_provider: Optional[str],
    duration_ms: int,
    ok: bool,
    error_class: Optional[str] = None,
    ts: Optional[float] = None,
) -> None:
    """记一条 vision call。**永不抛**——主流程不能因为 metrics 失败而挂。"""
    if not task_name:
        return
    try:
        with _conn() as c:
            c.execute(
                "INSERT INTO vision_calls "
                "(ts, task_name, model, api_provider, duration_ms, ok, error_class) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    float(ts if ts is not None else time.time()),
                    str(task_name),
                    str(model) if model else None,
                    str(api_provider) if api_provider else None,
                    int(duration_ms),
                    1 if ok else 0,
                    str(error_class) if error_class else None,
                ),
            )
    except Exception as e:
        logger.debug("[vision_metrics] record 失败 %s", e)


@dataclass(frozen=True)
class TaskSummary:
    task_name: str
    model: Optional[str]
    count: int
    ok_count: int
    fail_count: int
    p50_ms: int
    p95_ms: int
    p99_ms: int
    avg_ms: int
    max_ms: int

    @property
    def ok_rate(self) -> float:
        return self.ok_count / self.count if self.count else 0.0


def _percentile(sorted_vals: List[int], pct: float) -> int:
    if not sorted_vals:
        return 0
    idx = int(round((pct / 100.0) * (len(sorted_vals) - 1)))
    return sorted_vals[max(0, min(len(sorted_vals) - 1, idx))]


def summary(
    *,
    since_sec: float = 24 * 3600,
    task_name: Optional[str] = None,
) -> List[TaskSummary]:
    """按 (task_name, model) 分桶聚合统计。``since_sec`` 倒推。

    用法：
      - dashboard：``summary(since_sec=3600)`` 看上小时
      - 容量评估：``summary(since_sec=86400)`` 看一天
      - 单任务排查：``summary(task_name="input_verify")``
    """
    cutoff = time.time() - since_sec
    sql = (
        "SELECT task_name, model, duration_ms, ok "
        "FROM vision_calls WHERE ts >= ?"
    )
    params: List[Any] = [cutoff]
    if task_name:
        sql += " AND task_name = ?"
        params.append(task_name)

    try:
        with _conn() as c:
            rows = c.execute(sql, params).fetchall()
    except Exception as e:
        logger.debug("[vision_metrics] summary query 失败 %s", e)
        return []

    # 分桶 (task, model) → list of (duration_ms, ok)
    buckets: Dict[tuple, List[tuple]] = {}
    for tn, mdl, dur, ok in rows:
        key = (tn, mdl)
        buckets.setdefault(key, []).append((int(dur), int(ok)))

    out: List[TaskSummary] = []
    for (tn, mdl), entries in buckets.items():
        durs = sorted(d for d, _ in entries)
        ok_count = sum(1 for _, o in entries if o)
        out.append(TaskSummary(
            task_name=tn,
            model=mdl,
            count=len(entries),
            ok_count=ok_count,
            fail_count=len(entries) - ok_count,
            p50_ms=_percentile(durs, 50),
            p95_ms=_percentile(durs, 95),
            p99_ms=_percentile(durs, 99),
            avg_ms=int(sum(durs) / len(durs)) if durs else 0,
            max_ms=max(durs) if durs else 0,
        ))
    out.sort(key=lambda s: (-s.count, s.task_name))
    return out


def error_breakdown(
    *,
    since_sec: float = 24 * 3600,
    task_name: Optional[str] = None,
) -> Dict[str, int]:
    """失败原因分桶——快速回答"哪类 error 多"。"""
    cutoff = time.time() - since_sec
    sql = (
        "SELECT error_class, COUNT(*) FROM vision_calls "
        "WHERE ts >= ? AND ok = 0"
    )
    params: List[Any] = [cutoff]
    if task_name:
        sql += " AND task_name = ?"
        params.append(task_name)
    sql += " GROUP BY error_class"
    try:
        with _conn() as c:
            rows = c.execute(sql, params).fetchall()
    except Exception:
        return {}
    return {(ec or "unknown"): int(n) for ec, n in rows}


def reset_for_test() -> None:
    """测试用：清表。"""
    try:
        _ensure_init()
        with _conn() as c:
            c.execute("DELETE FROM vision_calls")
    except Exception:
        pass


__all__ = [
    "TaskSummary",
    "configure",
    "record",
    "summary",
    "error_breakdown",
    "reset_for_test",
]
