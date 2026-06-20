"""Web 管理面板用户存储 — SQLite + PBKDF2 密码哈希"""

import hashlib
import hmac
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

ROLE_MASTER = "master"
ROLE_ADMIN = "admin"
ROLE_VIEWER = "viewer"
ROLE_AGENT = "agent"

ROLE_LABELS = {
    ROLE_MASTER: "主帐号（全部权限）",
    ROLE_ADMIN: "管理员（编辑权限）",
    ROLE_VIEWER: "只读观察员",
    ROLE_AGENT: "坐席（仅聊天工作台）",
}

# ── UI 模式 ─────────────────────────────────────────────────
UI_MODE_SIMPLE = "simple"
UI_MODE_FULL = "full"

UI_MODE_LABELS = {
    UI_MODE_SIMPLE: "简洁模式",
    UI_MODE_FULL:   "完整模式",
}

SIMPLE_MODE_CORE_PAGES = {"cases", "knowledge", "ch", "learner"}

SIMPLE_MODE_MORE_PAGES = {"dash", "analytics", "audit", "episodic", "crisis_audit", "help", "line_rpa"}

ROLE_DEFAULT_UI_MODE = {
    ROLE_MASTER: UI_MODE_SIMPLE,
    ROLE_ADMIN:  UI_MODE_SIMPLE,
    ROLE_VIEWER: UI_MODE_SIMPLE,
    ROLE_AGENT:  UI_MODE_SIMPLE,
}


def resolve_ui_mode(cookie_val: str, role: str) -> str:
    """Determine effective ui_mode from cookie preference + role default."""
    if cookie_val in (UI_MODE_SIMPLE, UI_MODE_FULL):
        return cookie_val
    return ROLE_DEFAULT_UI_MODE.get(role, UI_MODE_SIMPLE)


def is_page_visible_in_simple(page_key: str) -> bool:
    """Whether a page shows in simple-mode sidebar (core or 'more' fold)."""
    return page_key in SIMPLE_MODE_CORE_PAGES or page_key in SIMPLE_MODE_MORE_PAGES

# ── 页面与写入权限 ──────────────────────────────────────────
PAGE_PERMISSIONS = {
    "dash":       {ROLE_MASTER, ROLE_ADMIN, ROLE_VIEWER},
    "tpl":        {ROLE_MASTER, ROLE_ADMIN},
    "ch":         {ROLE_MASTER, ROLE_ADMIN, ROLE_VIEWER},
    "strategies": {ROLE_MASTER, ROLE_ADMIN},
    "audit":      {ROLE_MASTER, ROLE_ADMIN, ROLE_VIEWER},
    "diff":       {ROLE_MASTER, ROLE_ADMIN},
    "logs":       {ROLE_MASTER, ROLE_ADMIN, ROLE_VIEWER},
    "analytics":  {ROLE_MASTER, ROLE_ADMIN, ROLE_VIEWER},
    "help":       {ROLE_MASTER, ROLE_ADMIN, ROLE_VIEWER},
    "users":      {ROLE_MASTER},
    "settings":   {ROLE_MASTER},
    "import":     {ROLE_MASTER},
    "export":     {ROLE_MASTER},
    "cases":      {ROLE_MASTER, ROLE_ADMIN, ROLE_VIEWER},
    "episodic":   {ROLE_MASTER, ROLE_ADMIN, ROLE_VIEWER},
    "crisis_audit": {ROLE_MASTER, ROLE_ADMIN, ROLE_VIEWER},
    "care":       {ROLE_MASTER, ROLE_ADMIN, ROLE_VIEWER},
    "monetization": {ROLE_MASTER, ROLE_ADMIN},   # 营收/变现数据：仅主帐号+管理员
    "line_rpa":   {ROLE_MASTER, ROLE_ADMIN, ROLE_VIEWER},
    "personas":   {ROLE_MASTER, ROLE_ADMIN, ROLE_VIEWER},
    # 坐席工作台（统一收件箱）：master/admin/agent 可用（viewer 只读不接管）
    "workspace":  {ROLE_MASTER, ROLE_ADMIN, ROLE_AGENT},
}

WRITE_PERMISSIONS = {
    "edit_template":  {ROLE_MASTER, ROLE_ADMIN},
    "edit_channel":   {ROLE_MASTER, ROLE_ADMIN},
    "edit_strategy":  {ROLE_MASTER, ROLE_ADMIN},
    "episodic_memory": {ROLE_MASTER, ROLE_ADMIN},
    "manage_users":   {ROLE_MASTER},
    "manage_settings":{ROLE_MASTER},
    "import_export":  {ROLE_MASTER},
    "edit_persona":   {ROLE_MASTER, ROLE_ADMIN},
    "manage_ops":     {ROLE_MASTER, ROLE_ADMIN},  # E2：确认/指派运维事件
}


def _hash_pw(password: str, salt: bytes = None) -> tuple:
    if salt is None:
        salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return salt, dk


class WebUserStore:
    _DDL = """
    CREATE TABLE IF NOT EXISTS web_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        pw_salt BLOB NOT NULL,
        pw_hash BLOB NOT NULL,
        role TEXT NOT NULL DEFAULT 'viewer',
        display_name TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        last_login TEXT,
        enabled INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS web_sessions (
        jti        TEXT PRIMARY KEY,
        username   TEXT NOT NULL,
        role       TEXT NOT NULL DEFAULT '',
        ip         TEXT DEFAULT '',
        user_agent TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        last_seen  TEXT NOT NULL,
        revoked    INTEGER DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_ws_user ON web_sessions(username);
    """

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._conn.executescript(self._DDL)
        self._conn.commit()

    # ── Session 管理 ──────────────────────────────────────────

    def create_session(self, username: str, role: str, ip: str = "",
                       user_agent: str = "") -> str:
        """创建新 session 记录，返回 jti（唯一 session 标识符）"""
        import uuid as _uuid
        jti = _uuid.uuid4().hex
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            self._conn.execute(
                "INSERT INTO web_sessions(jti,username,role,ip,user_agent,created_at,last_seen)"
                " VALUES(?,?,?,?,?,?,?)",
                (jti, username, role, ip[:64], (user_agent or "")[:200], now, now),
            )
            self._conn.commit()
        return jti

    def touch_session(self, jti: str) -> bool:
        """更新 session 最后活跃时间，返回该 session 是否有效"""
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT revoked FROM web_sessions WHERE jti=?", (jti,)
                ).fetchone()
                if not row or row["revoked"]:
                    return False
                self._conn.execute(
                    "UPDATE web_sessions SET last_seen=? WHERE jti=?",
                    (time.strftime("%Y-%m-%d %H:%M:%S"), jti),
                )
                self._conn.commit()
            except (sqlite3.OperationalError, sqlite3.InterfaceError):
                try:
                    self._reconnect()
                    return True
                except Exception:
                    return True
        return True

    def _reconnect(self):
        """Re-open the SQLite connection after an InterfaceError."""
        try:
            self._conn.close()
        except Exception:
            pass
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def revoke_session(self, jti: str):
        """撤销指定 session（强制下线）"""
        with self._lock:
            self._conn.execute(
                "UPDATE web_sessions SET revoked=1 WHERE jti=?", (jti,)
            )
            self._conn.commit()

    def revoke_all_sessions(self, username: str = None):
        """撤销所有 session 或指定用户的所有 session"""
        with self._lock:
            if username:
                self._conn.execute(
                    "UPDATE web_sessions SET revoked=1 WHERE username=?", (username,)
                )
            else:
                self._conn.execute("UPDATE web_sessions SET revoked=1")
            self._conn.commit()

    def list_sessions(self, include_revoked: bool = False) -> List[Dict]:
        """列出所有活跃 session（按最后活跃时间倒序）"""
        sql = (
            "SELECT jti,username,role,ip,user_agent,created_at,last_seen,revoked "
            "FROM web_sessions "
            + ("" if include_revoked else "WHERE revoked=0 ")
            + "ORDER BY last_seen DESC LIMIT 100"
        )
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [dict(r) for r in rows]

    def cleanup_old_sessions(self, days: int = 30):
        """清理超过 N 天未活跃的 session"""
        with self._lock:
            self._conn.execute(
                "DELETE FROM web_sessions WHERE last_seen < datetime('now', ?)",
                (f"-{days} days",),
            )
            self._conn.commit()

    def _ensure_master(self, username: str, password: str):
        """确保至少存在一个主帐号；若已存在则跳过"""
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM web_users WHERE role=?", (ROLE_MASTER,)
            ).fetchone()
        if row:
            return
        self.create_user(username, password, ROLE_MASTER, display_name="管理员")

    def create_user(self, username: str, password: str, role: str = ROLE_VIEWER,
                    display_name: str = "") -> Optional[Dict]:
        if role not in ROLE_LABELS:
            return None
        salt, hashed = _hash_pw(password)
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO web_users (username, pw_salt, pw_hash, role, display_name, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (username, salt, hashed, role, display_name or username,
                     time.strftime("%Y-%m-%d %H:%M:%S"))
                )
                self._conn.commit()
            return self.get_user(username)
        except sqlite3.IntegrityError:
            return None

    def verify(self, username: str, password: str) -> Optional[Dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM web_users WHERE username=? AND enabled=1", (username,)
            ).fetchone()
            if not row:
                return None
            salt = row["pw_salt"]
            expected = row["pw_hash"]
            _, actual = _hash_pw(password, salt)
            if not hmac.compare_digest(actual, expected):
                return None
            self._conn.execute(
                "UPDATE web_users SET last_login=? WHERE id=?",
                (time.strftime("%Y-%m-%d %H:%M:%S"), row["id"])
            )
            self._conn.commit()
            return dict(row)

    def get_user(self, username: str) -> Optional[Dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM web_users WHERE username=?", (username,)
            ).fetchone()
        return dict(row) if row else None

    def get_user_by_id(self, user_id: int) -> Optional[Dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, username, role, display_name, enabled FROM web_users WHERE id=?",
                (user_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_users(self) -> List[Dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, username, role, display_name, created_at, last_login, enabled "
                "FROM web_users ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def update_user(self, user_id: int, role: str = None, enabled: bool = None,
                    password: str = None, display_name: str = None) -> bool:
        sets, params = [], []
        if role and role in ROLE_LABELS:
            sets.append("role=?"); params.append(role)
        if enabled is not None:
            sets.append("enabled=?"); params.append(1 if enabled else 0)
        if password:
            salt, hashed = _hash_pw(password)
            sets.append("pw_salt=?"); params.append(salt)
            sets.append("pw_hash=?"); params.append(hashed)
        if display_name is not None:
            sets.append("display_name=?"); params.append(display_name)
        if not sets:
            return False
        params.append(user_id)
        with self._lock:
            self._conn.execute(f"UPDATE web_users SET {','.join(sets)} WHERE id=?", params)
            self._conn.commit()
        return True

    def delete_user(self, user_id: int) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT role FROM web_users WHERE id=?", (user_id,)).fetchone()
            if not row or row["role"] == ROLE_MASTER:
                return False
            self._conn.execute("DELETE FROM web_users WHERE id=?", (user_id,))
            self._conn.commit()
        return True

    def can_access_page(self, role: str, page_key: str) -> bool:
        allowed = PAGE_PERMISSIONS.get(page_key)
        if allowed is None:
            return role == ROLE_MASTER
        return role in allowed

    def can_write(self, role: str, permission: str) -> bool:
        allowed = WRITE_PERMISSIONS.get(permission)
        if allowed is None:
            return role == ROLE_MASTER
        return role in allowed

    def user_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) c FROM web_users").fetchone()
        return row["c"] if row else 0
