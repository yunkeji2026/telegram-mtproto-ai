"""
管理员用户存储
- SQLite 持久化
- PBKDF2-HMAC-SHA256 密码哈希（10万次迭代 + 随机 salt，无需第三方库）
- 角色：superadmin（可管理用户）/ admin（普通管理员）
- 引导模式：无用户时降级到 config.yaml 验证，验证成功后自动迁移
"""
import hashlib
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional


_PBKDF2_ITER = 100_000   # 密钥拉伸迭代次数（NIST 推荐 ≥ 100k）


class AdminUserStore:
    """管理员账户存储与验证"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        with self._conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS admin_users (
                id         TEXT PRIMARY KEY,
                username   TEXT UNIQUE NOT NULL,
                password   TEXT NOT NULL,
                salt       TEXT NOT NULL,
                role       TEXT DEFAULT 'admin',
                created_at TEXT NOT NULL,
                last_login TEXT,
                enabled    INTEGER DEFAULT 1
            );
            """)

    # ── 密码工具 ─────────────────────────────────────────

    @staticmethod
    def _make_salt() -> str:
        return os.urandom(16).hex()

    @staticmethod
    def _hash(password: str, salt: str) -> str:
        return hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            _PBKDF2_ITER,
        ).hex()

    # ── 用户 CRUD ─────────────────────────────────────────

    def has_users(self) -> bool:
        """是否已有启用的账户（用于判断是否需要降级到 config.yaml）"""
        with self._conn() as c:
            n = c.execute(
                "SELECT COUNT(*) FROM admin_users WHERE enabled=1"
            ).fetchone()[0]
        return n > 0

    def create_user(self, username: str, password: str,
                    role: str = "admin") -> str:
        uid  = str(uuid.uuid4())[:8]
        salt = self._make_salt()
        now  = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as c:
            c.execute(
                "INSERT INTO admin_users (id,username,password,salt,role,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (uid, username, self._hash(password, salt), salt, role, now),
            )
        return uid

    def verify(self, username: str, password: str) -> Optional[Dict]:
        """
        验证用户名/密码。
        成功返回用户信息 dict（含 id/username/role），失败返回 None。
        """
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM admin_users WHERE username=? AND enabled=1",
                (username,),
            ).fetchone()
        if not row:
            return None
        if self._hash(password, row["salt"]) != row["password"]:
            return None
        # 更新最后登录时间
        with self._conn() as c:
            c.execute(
                "UPDATE admin_users SET last_login=? WHERE id=?",
                (time.strftime("%Y-%m-%dT%H:%M:%S"), row["id"]),
            )
        return dict(row)

    def get_user(self, user_id: str) -> Optional[Dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT id,username,role,created_at,last_login,enabled "
                "FROM admin_users WHERE id=?",
                (user_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_users(self) -> List[Dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id,username,role,created_at,last_login,enabled "
                "FROM admin_users ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def update_password(self, user_id: str, new_password: str) -> bool:
        salt = self._make_salt()
        with self._conn() as c:
            c.execute(
                "UPDATE admin_users SET password=?,salt=? WHERE id=?",
                (self._hash(new_password, salt), salt, user_id),
            )
        return True

    def update_role(self, user_id: str, role: str) -> bool:
        if role not in ("admin", "superadmin"):
            return False
        with self._conn() as c:
            c.execute("UPDATE admin_users SET role=? WHERE id=?", (role, user_id))
        return True

    def toggle_enabled(self, user_id: str) -> bool:
        with self._conn() as c:
            c.execute(
                "UPDATE admin_users SET enabled=1-enabled WHERE id=?", (user_id,)
            )
        return True

    def delete_user(self, user_id: str) -> bool:
        with self._conn() as c:
            c.execute("DELETE FROM admin_users WHERE id=?", (user_id,))
        return True

    def superadmin_count(self) -> int:
        """在线超级管理员数量（用于防止删除最后一个超级管理员）"""
        with self._conn() as c:
            return c.execute(
                "SELECT COUNT(*) FROM admin_users WHERE role='superadmin' AND enabled=1"
            ).fetchone()[0]

    def ensure_bootstrap(self, username: str, password: str,
                         role: str = "superadmin"):
        """
        引导初始化：无用户时创建第一个超级管理员账户。
        通常在检测到 config.yaml 密码验证通过后调用，实现零迁移成本。
        """
        if not self.has_users():
            self.create_user(username, password, role)
