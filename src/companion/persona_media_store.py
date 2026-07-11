"""每人设「相册/媒体」注册表（SQLite，图片+视频）。

给「每个人设一个相册、后台上传图/视频、按关键词触发调用」建持久层：人设身份仍在
``PersonaManager`` 的 YAML profile，**媒体**（二进制落 /static、高频命中计数、触发词元数据）
落 DB（与 inbox/audit store 同型）。一行 = 一个可发送的媒体条目。

设计（对齐 ``translation_trend_store`` 的线程安全 SQLite + 模块级单例范式）：
- 线程安全（单连接 + Lock，``check_same_thread=False``，WAL）；``:memory:`` 供单测。
- CRUD + ``record_hit``（命中计数/轮播避重用）+ ``find_by_sha``（上传去重）。
- JSON 列（triggers/tags/caption_i18n）在读出时反序列化为 Python 对象。
- **匹配/挑选逻辑不在本模块**（那是纯函数，见 ``persona_media.py``）——本模块只管存取。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MEDIA_PHOTO = "photo"
MEDIA_VIDEO = "video"
_VALID_MEDIA = (MEDIA_PHOTO, MEDIA_VIDEO)

# 默认库路径（与其它 config/*.db 同处）；main.py 启动期可用 configure_* 显式指定。
DEFAULT_DB_PATH = "config/persona_media.db"

_DDL = """
CREATE TABLE IF NOT EXISTS persona_media (
    id             TEXT NOT NULL PRIMARY KEY,
    persona_id     TEXT NOT NULL,
    media_type     TEXT NOT NULL DEFAULT 'photo',
    file_path      TEXT NOT NULL DEFAULT '',
    url            TEXT NOT NULL DEFAULT '',
    thumb_url      TEXT NOT NULL DEFAULT '',
    triggers       TEXT NOT NULL DEFAULT '[]',
    caption        TEXT NOT NULL DEFAULT '',
    caption_i18n   TEXT NOT NULL DEFAULT '{}',
    tags           TEXT NOT NULL DEFAULT '[]',
    weight         INTEGER NOT NULL DEFAULT 1,
    enabled        INTEGER NOT NULL DEFAULT 1,
    tier           TEXT NOT NULL DEFAULT '',
    min_bond_level INTEGER NOT NULL DEFAULT 0,
    bytes          INTEGER NOT NULL DEFAULT 0,
    width          INTEGER NOT NULL DEFAULT 0,
    height         INTEGER NOT NULL DEFAULT 0,
    duration_ms    INTEGER NOT NULL DEFAULT 0,
    sha256         TEXT NOT NULL DEFAULT '',
    hits           INTEGER NOT NULL DEFAULT 0,
    last_sent_at   REAL NOT NULL DEFAULT 0,
    created_by     TEXT NOT NULL DEFAULT '',
    created_at     REAL NOT NULL DEFAULT 0,
    updated_at     REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pmedia_persona ON persona_media(persona_id, enabled);
CREATE INDEX IF NOT EXISTS idx_pmedia_sha ON persona_media(persona_id, sha256);
"""

# update() 允许热改的元数据字段（file_path/url/sha 等身份字段不可改——换文件请删了重传）。
_UPDATABLE = {
    "media_type", "triggers", "caption", "caption_i18n", "tags", "weight",
    "enabled", "tier", "min_bond_level", "thumb_url", "duration_ms",
    "width", "height",
}
_JSON_COLS = {"triggers", "tags", "caption_i18n"}


def _dumps(v: Any, default: str) -> str:
    try:
        return json.dumps(v, ensure_ascii=False)
    except Exception:
        return default


class PersonaMediaStore:
    """每人设媒体注册表（线程安全 SQLite）。"""

    def __init__(self, db_path: Any = ":memory:") -> None:
        self._is_mem = str(db_path) == ":memory:"
        if not self._is_mem:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            if not self._is_mem:
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_DDL)
            self._conn.commit()

    @staticmethod
    def _row_to_dict(r: sqlite3.Row) -> Dict[str, Any]:
        d = dict(r)
        for col in _JSON_COLS:
            raw = d.get(col)
            try:
                d[col] = json.loads(raw) if isinstance(raw, str) and raw else (
                    {} if col == "caption_i18n" else [])
            except Exception:
                d[col] = {} if col == "caption_i18n" else []
        d["enabled"] = bool(d.get("enabled"))
        return d

    def add(
        self, persona_id: str, media_type: str, file_path: str, url: str, *,
        thumb_url: str = "", triggers: Optional[List[str]] = None,
        caption: str = "", caption_i18n: Optional[Dict[str, str]] = None,
        tags: Optional[List[str]] = None, weight: int = 1, enabled: bool = True,
        tier: str = "", min_bond_level: int = 0, bytes_: int = 0,
        width: int = 0, height: int = 0, duration_ms: int = 0, sha256: str = "",
        created_by: str = "", now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """新增一个媒体条目，返回落库后的行（dict）。"""
        mt = str(media_type or "").strip().lower()
        if mt not in _VALID_MEDIA:
            mt = MEDIA_PHOTO
        mid = uuid.uuid4().hex
        ts = float(now if now is not None else time.time())
        row = (
            mid, str(persona_id or ""), mt, str(file_path or ""), str(url or ""),
            str(thumb_url or ""), _dumps(list(triggers or []), "[]"),
            str(caption or ""), _dumps(dict(caption_i18n or {}), "{}"),
            _dumps(list(tags or []), "[]"), int(weight or 1),
            1 if enabled else 0, str(tier or ""), int(min_bond_level or 0),
            int(bytes_ or 0), int(width or 0), int(height or 0),
            int(duration_ms or 0), str(sha256 or ""), 0, 0.0,
            str(created_by or ""), ts, ts,
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO persona_media (id, persona_id, media_type, file_path, "
                "url, thumb_url, triggers, caption, caption_i18n, tags, weight, "
                "enabled, tier, min_bond_level, bytes, width, height, duration_ms, "
                "sha256, hits, last_sent_at, created_by, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
            self._conn.commit()
        got = self.get(mid)
        return got or {}

    def get(self, media_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM persona_media WHERE id = ?", (str(media_id or ""),)
            ).fetchone()
        return self._row_to_dict(r) if r else None

    def list(
        self, persona_id: Optional[str] = None, *,
        enabled_only: bool = False, media_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """列出媒体条目（按 created_at 升序）。persona_id=None 列全部。"""
        where, args = [], []
        if persona_id is not None:
            where.append("persona_id = ?")
            args.append(str(persona_id))
        if enabled_only:
            where.append("enabled = 1")
        if media_type:
            where.append("media_type = ?")
            args.append(str(media_type).strip().lower())
        sql = "SELECT * FROM persona_media"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at ASC, id ASC"
        with self._lock:
            rows = self._conn.execute(sql, tuple(args)).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def find_by_sha(self, persona_id: str, sha256: str) -> Optional[Dict[str, Any]]:
        """按 (persona_id, sha256) 查已存在条目（上传去重）。"""
        if not sha256:
            return None
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM persona_media WHERE persona_id = ? AND sha256 = ? LIMIT 1",
                (str(persona_id or ""), str(sha256)),
            ).fetchone()
        return self._row_to_dict(r) if r else None

    def update(self, media_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        """热改元数据（仅 ``_UPDATABLE`` 白名单字段）；返回更新后的行。"""
        sets, args = [], []
        for k, v in fields.items():
            if k not in _UPDATABLE:
                continue
            if k in _JSON_COLS:
                v = _dumps(v, "{}" if k == "caption_i18n" else "[]")
            elif k == "enabled":
                v = 1 if v else 0
            elif k in ("weight", "min_bond_level", "duration_ms", "width", "height"):
                v = int(v or 0)
            sets.append(f"{k} = ?")
            args.append(v)
        if not sets:
            return self.get(media_id)
        sets.append("updated_at = ?")
        args.append(time.time())
        args.append(str(media_id or ""))
        with self._lock:
            self._conn.execute(
                f"UPDATE persona_media SET {', '.join(sets)} WHERE id = ?", tuple(args))
            self._conn.commit()
        return self.get(media_id)

    def delete(self, media_id: str) -> Optional[Dict[str, Any]]:
        """删除条目，返回被删的行（供调用方顺手删磁盘文件）。"""
        row = self.get(media_id)
        if row is None:
            return None
        with self._lock:
            self._conn.execute(
                "DELETE FROM persona_media WHERE id = ?", (str(media_id),))
            self._conn.commit()
        return row

    def record_hit(self, media_id: str, now: Optional[float] = None) -> None:
        """命中一次（hits+1、last_sent_at=now），供轮播避重 + 内容分析。绝不抛。"""
        ts = float(now if now is not None else time.time())
        try:
            with self._lock:
                self._conn.execute(
                    "UPDATE persona_media SET hits = hits + 1, last_sent_at = ? "
                    "WHERE id = ?", (ts, str(media_id or "")))
                self._conn.commit()
        except Exception:
            logger.debug("[persona_media] record_hit 失败（已忽略）", exc_info=True)

    def stats(self, persona_id: Optional[str] = None) -> Dict[str, Any]:
        """条目计数（总/按类型/启用数）。"""
        where, args = [], []
        if persona_id is not None:
            where.append("persona_id = ?")
            args.append(str(persona_id))
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT media_type, COUNT(*) c, SUM(enabled) e FROM persona_media"
                f"{clause} GROUP BY media_type", tuple(args)).fetchall()
        out = {"total": 0, "enabled": 0, "photo": 0, "video": 0}
        for r in rows:
            mt = str(r["media_type"] or "")
            c = int(r["c"] or 0)
            out["total"] += c
            out["enabled"] += int(r["e"] or 0)
            if mt in out:
                out[mt] = c
        return out

    def analytics(
        self, persona_id: Optional[str] = None, *, top_n: int = 8,
    ) -> Dict[str, Any]:
        """观测聚合：计数 + 命中总数 + 命中最高的 Top-N 条目（供 ops 看板/metrics）。

        persona_id=None → 跨全部人设聚合（metrics 端点用）；否则限定单人设（人设编辑器可用）。
        """
        out: Dict[str, Any] = dict(self.stats(persona_id))
        where, args = [], []
        if persona_id is not None:
            where.append("persona_id = ?")
            args.append(str(persona_id))
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            trow = self._conn.execute(
                f"SELECT COALESCE(SUM(hits),0) th FROM persona_media{clause}",
                tuple(args)).fetchone()
            top_rows = self._conn.execute(
                f"SELECT * FROM persona_media{clause} "
                f"{'AND' if where else 'WHERE'} hits > 0 "
                f"ORDER BY hits DESC, last_sent_at DESC LIMIT ?",
                tuple(args) + (int(max(0, top_n)),)).fetchall()
        out["total_hits"] = int(trow["th"] or 0) if trow else 0
        out["top"] = [
            {
                "id": d.get("id"), "persona_id": d.get("persona_id"),
                "media_type": d.get("media_type"), "hits": d.get("hits") or 0,
                "caption": d.get("caption") or "", "triggers": d.get("triggers") or [],
            }
            for d in (self._row_to_dict(r) for r in top_rows)
        ]
        return out


# ── 模块级单例（懒建；main.py 可用 configure_* 显式指定库路径）───────────────
_STORE: Optional[PersonaMediaStore] = None
_DB_PATH: str = DEFAULT_DB_PATH
_CFG_LOCK = threading.Lock()


def configure_persona_media_store(db_path: Any = DEFAULT_DB_PATH) -> Optional[PersonaMediaStore]:
    """启动期装配（幂等）。指定库路径并建库。"""
    global _STORE, _DB_PATH
    with _CFG_LOCK:
        _DB_PATH = str(db_path)
        if _STORE is None:
            try:
                _STORE = PersonaMediaStore(_DB_PATH)
            except Exception:
                logger.warning("[persona_media] 建库失败", exc_info=True)
                _STORE = None
        return _STORE


def get_persona_media_store() -> Optional[PersonaMediaStore]:
    """取 store 单例（未配置则按默认路径懒建）。建库失败返回 None（调用方需容错）。"""
    global _STORE
    if _STORE is None:
        with _CFG_LOCK:
            if _STORE is None:
                try:
                    _STORE = PersonaMediaStore(_DB_PATH)
                except Exception:
                    logger.warning("[persona_media] 懒建库失败", exc_info=True)
                    _STORE = None
    return _STORE


def reset_persona_media_store() -> None:
    """测试钩子：清空单例。"""
    global _STORE
    with _CFG_LOCK:
        _STORE = None


__all__ = [
    "MEDIA_PHOTO", "MEDIA_VIDEO", "DEFAULT_DB_PATH", "PersonaMediaStore",
    "configure_persona_media_store", "get_persona_media_store",
    "reset_persona_media_store",
]
