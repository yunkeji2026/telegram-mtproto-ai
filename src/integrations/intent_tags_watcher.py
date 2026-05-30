"""P26-A: filesystem watcher for `config/intent_tags.yaml` → auto-reload.

Motivation:
    Before P26-A, operators had to POST `/api/rpa/intent-tags/reload` after
    editing the yaml file on disk (or use the admin UI which posts the same
    endpoint). For batch edits and external CI deployments, this is friction.

    With this module, the file is watched via `watchdog`; any modification
    triggers a debounced reload on a background thread.

Design:
    - Single shared `Observer` for the parent directory only (cheaper than
      one watcher per file; Observer scans the dir and we filter by path).
    - Debounce window (default 0.8s) collapses rapid editor saves
      (vim swp → write, IDE atomic-replace) into one reload.
    - Idempotent start/stop — call twice safely; no-op when already running.
    - Lightweight: only stdlib threading + watchdog (already in deps).
    - On reload failure, logs at WARNING (no crash) and keeps watching.
    - Counter exposed via `get_reload_stats()` → Prometheus.

Usage:
    from src.integrations.intent_tags_watcher import start_watcher, stop_watcher
    start_watcher(debounce_sec=0.8)
    ...
    stop_watcher()

Test hook:
    `trigger_reload_now()` bypasses debounce (synchronous reload).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Module state
# ──────────────────────────────────────────────────────────────────────
_observer = None  # type: Optional[object]   # watchdog.observers.Observer
_lock = threading.Lock()
_pending_timer: Optional[threading.Timer] = None
_watched_path: Optional[Path] = None
_debounce_sec: float = 0.8

# Stats (Prometheus)
_stats = {
    "auto_reloads_total": 0,        # 成功的自动 reload 次数
    "auto_reload_failures": 0,      # reload 抛异常的次数
    "last_event_ts": 0.0,           # 最近一次文件 event 的时间（unix）
    "last_reload_ts": 0.0,          # 最近一次成功 reload 的时间
    "events_debounced": 0,          # 被去抖合并的事件数
}


def get_reload_stats() -> dict:
    """快照（不暴露内部 dict 引用 — 防呼叫方乱改）。"""
    return dict(_stats)


# ──────────────────────────────────────────────────────────────────────
# Debounced reload
# ──────────────────────────────────────────────────────────────────────
def _do_reload() -> None:
    """实际触发 rpa_shared.reload_intent_tags()，更新统计。"""
    try:
        # 延迟 import 避免循环依赖
        from src.integrations.rpa_shared import reload_intent_tags
        reload_intent_tags()
        _stats["auto_reloads_total"] += 1
        _stats["last_reload_ts"] = time.time()
        logger.info("intent_tags.yaml 自动 reload 成功（watchdog 触发）")
    except Exception:  # pragma: no cover — best-effort
        _stats["auto_reload_failures"] += 1
        logger.warning("intent_tags.yaml 自动 reload 失败", exc_info=True)


def _schedule_reload() -> None:
    """事件触发 → 取消旧 timer，启动新 timer（去抖）。"""
    global _pending_timer
    with _lock:
        _stats["last_event_ts"] = time.time()
        if _pending_timer is not None and _pending_timer.is_alive():
            _pending_timer.cancel()
            _stats["events_debounced"] += 1
        _pending_timer = threading.Timer(_debounce_sec, _do_reload)
        _pending_timer.daemon = True
        _pending_timer.start()


# ──────────────────────────────────────────────────────────────────────
# watchdog plumbing
# ──────────────────────────────────────────────────────────────────────
def _make_handler():
    """Lazy build handler (avoids watchdog import at module-load if disabled)."""
    from watchdog.events import FileSystemEventHandler

    class _Handler(FileSystemEventHandler):
        def __init__(self, target: Path) -> None:
            super().__init__()
            self._target_resolved = str(target.resolve()).lower()

        def _match(self, evt_path: str) -> bool:
            try:
                return os.path.normcase(os.path.realpath(evt_path)) == \
                       os.path.normcase(self._target_resolved)
            except Exception:
                return False

        def on_modified(self, event):
            if event.is_directory:
                return
            if self._match(event.src_path):
                _schedule_reload()

        def on_created(self, event):
            if event.is_directory:
                return
            if self._match(event.src_path):
                _schedule_reload()

        def on_moved(self, event):
            # 编辑器原子保存（write tmp → rename target）→ on_moved.dest_path == target
            if event.is_directory:
                return
            dest = getattr(event, "dest_path", "") or ""
            if self._match(dest):
                _schedule_reload()

    return _Handler


def start_watcher(debounce_sec: float = 0.8) -> bool:
    """启动监听。重复调用幂等（已在跑则 noop 并返回 True）。

    Returns True iff watcher is running (either started or already was).
    Returns False on setup failure (e.g. watchdog import error, missing dir).
    """
    global _observer, _watched_path, _debounce_sec
    with _lock:
        if _observer is not None:
            return True
        try:
            from src.integrations.rpa_shared import _intent_tags_yaml_path
            from watchdog.observers import Observer
        except Exception:
            logger.warning("watchdog 不可用，intent_tags 自动 reload 已禁用", exc_info=True)
            return False

        target = _intent_tags_yaml_path()
        # 监听目录（即使文件还不存在也能监听）
        parent = target.parent
        if not parent.is_dir():
            logger.warning("intent_tags watcher: parent dir not found: %s", parent)
            return False

        _debounce_sec = max(0.05, float(debounce_sec))
        _watched_path = target
        try:
            obs = Observer()
            obs.schedule(_make_handler()(target), str(parent), recursive=False)
            obs.daemon = True
            obs.start()
            _observer = obs
            logger.info("intent_tags 自动 reload watcher 启动: %s (debounce=%.2fs)",
                        target, _debounce_sec)
            return True
        except Exception:
            logger.exception("intent_tags watcher 启动失败")
            _observer = None
            return False


def stop_watcher() -> None:
    """优雅停止（用于关停 / 测试 teardown）。"""
    global _observer, _pending_timer
    with _lock:
        obs = _observer
        _observer = None
        if _pending_timer is not None:
            try:
                _pending_timer.cancel()
            except Exception:
                pass
            _pending_timer = None
    if obs is not None:
        try:
            obs.stop()
            obs.join(timeout=2.0)
        except Exception:
            pass


def trigger_reload_now() -> None:
    """测试钩子：跳过 debounce 直接执行同步 reload + 计数。"""
    _do_reload()


def is_running() -> bool:
    return _observer is not None
