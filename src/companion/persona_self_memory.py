"""E3 · 人设自身的长期记忆（跨会话「见闻」，让人设像真人一样"有在过日子、会成长"）。

想法：真人跟很多人聊过之后会有"最近好多人问我大阪攻略"这种**跨对话的共性见闻**。
本模块给人设**级**（非某个会话）累积**去标识的话题计数**——只存话题词 + 次数，
**绝不关联任何具体客户 / 会话 id / 原文**（隐私红线）。频次够高(≥min_count)的话题
才会被自然提起，制造"见多识广"的真人感。

默认关（`companion.deep_persona.self_memory`）。纯 mapping 无依赖、可单测；store 轻量。
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 与 deep_persona._STOP 同域的高频停用（避免把虚词/语气词当"话题"）。
_STOP = frozenset([
    "什么", "怎么", "这个", "那个", "我们", "你们", "然后", "就是", "还有", "但是",
    "可以", "不是", "这样", "知道", "觉得", "真的", "一个", "有点", "哈哈", "嗯嗯",
    "现在", "今天", "明天", "时候", "自己", "喜欢", "问题", "感觉",
])
_MAX_TOPICS = 200


def extract_self_topic(text: str) -> Optional[str]:
    """从一条消息粗抽一个**去标识的话题词**（2-6 字中文名词性片段）。

    极轻启发式：取最长的非停用中文片段（跳过纯数字/英文/标点）。抽不出 → None。
    目的不是精准，而是让**反复出现**的话题（跨很多人）浮现；单次噪声不影响（靠计数门槛）。
    """
    t = str(text or "").strip()
    if not t or len(t) < 2:
        return None
    best = None
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", t):
        for L in range(min(6, len(run)), 1, -1):
            for i in range(0, len(run) - L + 1):
                frag = run[i:i + L]
                if frag in _STOP:
                    continue
                if best is None or len(frag) > len(best):
                    best = frag
                break
            if best and len(best) >= 3:
                break
    return best


def format_self_memory(topics: List[str]) -> str:
    """频繁话题 → 提示词块。空 → ""。硬约束：绝不透露"具体是谁问的"。"""
    ts = [str(t).strip() for t in (topics or []) if str(t).strip()]
    if not ts:
        return ""
    return (
        "【你最近的见闻（跨很多人聊下来的共性）】最近好多人跟你聊到：" + "、".join(ts[:4])
        + "——可在合适时自然提一句（像真人有见识那样），但**绝不透露是具体谁问的、也不串具体某人的信息**。"
    )


class PersonaSelfMemoryStore:
    """人设级去标识话题计数（独立轻量 SQLite）。"""

    def __init__(self, db_path: str | Path):
        self._path = str(db_path)
        self._lock = threading.RLock()
        self._ensure()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def _ensure(self) -> None:
        try:
            with self._lock, self._conn() as c:
                c.execute(
                    """CREATE TABLE IF NOT EXISTS persona_self_topics (
                        persona_id TEXT NOT NULL,
                        topic TEXT NOT NULL,
                        count INTEGER NOT NULL DEFAULT 0,
                        last_ts REAL NOT NULL DEFAULT 0,
                        PRIMARY KEY (persona_id, topic)
                    )"""
                )
        except Exception:
            logger.debug("[persona_self_memory] ensure failed", exc_info=True)

    def record_topic(self, persona_id: str, topic: str) -> None:
        pid = str(persona_id or "").strip()
        tp = str(topic or "").strip()
        if not pid or not tp:
            return
        try:
            with self._lock, self._conn() as c:
                c.execute(
                    "INSERT INTO persona_self_topics(persona_id, topic, count, last_ts) "
                    "VALUES(?,?,1,?) ON CONFLICT(persona_id, topic) DO UPDATE SET "
                    "count=count+1, last_ts=excluded.last_ts",
                    (pid, tp, time.time()),
                )
                # 上限裁剪：每人设只保留最高频的 N 个话题
                c.execute(
                    "DELETE FROM persona_self_topics WHERE persona_id=? AND topic NOT IN "
                    "(SELECT topic FROM persona_self_topics WHERE persona_id=? "
                    "ORDER BY count DESC, last_ts DESC LIMIT ?)",
                    (pid, pid, _MAX_TOPICS),
                )
        except Exception:
            logger.debug("[persona_self_memory] record failed", exc_info=True)

    def top_topics(self, persona_id: str, *, min_count: int = 3, k: int = 4) -> List[str]:
        pid = str(persona_id or "").strip()
        if not pid:
            return []
        try:
            with self._lock, self._conn() as c:
                rows = c.execute(
                    "SELECT topic FROM persona_self_topics WHERE persona_id=? AND count>=? "
                    "ORDER BY count DESC, last_ts DESC LIMIT ?",
                    (pid, int(min_count), int(k)),
                ).fetchall()
                return [str(r["topic"]) for r in rows]
        except Exception:
            return []


_SINGLETON: Optional[PersonaSelfMemoryStore] = None
_LOCK = threading.Lock()


def get_persona_self_memory(db_path: str | Path | None = None) -> Optional[PersonaSelfMemoryStore]:
    global _SINGLETON
    if _SINGLETON is None:
        if db_path is None:
            return None
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = PersonaSelfMemoryStore(db_path)
    return _SINGLETON


def reset_persona_self_memory() -> None:
    global _SINGLETON
    with _LOCK:
        _SINGLETON = None


__all__ = [
    "extract_self_topic", "format_self_memory",
    "PersonaSelfMemoryStore", "get_persona_self_memory", "reset_persona_self_memory",
]
