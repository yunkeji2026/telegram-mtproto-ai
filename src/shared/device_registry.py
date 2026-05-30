# -*- coding: utf-8 -*-
"""
统一设备注册表 — 两个项目共用 openclaw.db 的 devices 表。

表结构 (devices):
  serial        TEXT PK   — 当前 ADB serial（可变，USB 重插可能改变）
  hw_serial     TEXT      — 硬件 serial（稳定，来自 ro.serialno）
  android_id    TEXT      — Android ID（稳定指纹备用）
  model         TEXT      — 设备型号
  number        INTEGER   — 用户定义编号 (5, 7, 8, 9 ...)
  label         TEXT      — 内部简码 (IJ8, Q4N, VWN, XW8)
  alias         TEXT      — 编号别名 ("07号")
  location      TEXT      — 完整位置标签 ("主控-手机07")
  group_name    TEXT      — 分组名 ("主控", "W03", ...)
  wifi_ip       TEXT      — 最近 WiFi IP
  platform_messenger TEXT — Messenger account_id
  platform_line      TEXT — LINE account_id
  platform_whatsapp  TEXT — WhatsApp account_id
  wallpaper_hash     TEXT — 上次壁纸 hash，用于检测是否需要重部署
  wallpaper_set_at   REAL — 上次壁纸设置时间戳
  created_at    TEXT
  updated_at    TEXT

使用:
    from src.shared.device_registry import DeviceRegistryDB
    db = DeviceRegistryDB()
    db.upsert(serial="Q4N7AM7HMZGU4LZD", number=7, label="Q4N",
              location="主控-手机07", group_name="主控")
    info = db.get("Q4N7AM7HMZGU4LZD")
    all_devices = db.all()
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

# openclaw.db 由两个项目共用
_DEFAULT_DB = Path(__file__).resolve().parents[3] / "mobile-auto0423" / "data" / "openclaw.db"

_DDL = """
CREATE TABLE IF NOT EXISTS devices (
    serial            TEXT PRIMARY KEY,
    hw_serial         TEXT DEFAULT '',
    android_id        TEXT DEFAULT '',
    model             TEXT DEFAULT '',
    number            INTEGER DEFAULT 0,
    label             TEXT DEFAULT '',
    alias             TEXT DEFAULT '',
    location          TEXT DEFAULT '',
    group_name        TEXT DEFAULT '',
    wifi_ip           TEXT DEFAULT '',
    platform_messenger TEXT DEFAULT '',
    platform_line      TEXT DEFAULT '',
    platform_whatsapp  TEXT DEFAULT '',
    wallpaper_hash     TEXT DEFAULT '',
    wallpaper_set_at   REAL DEFAULT 0,
    created_at        TEXT DEFAULT (datetime('now','localtime')),
    updated_at        TEXT DEFAULT (datetime('now','localtime'))
);
"""

_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS devices_updated_at
AFTER UPDATE ON devices
BEGIN
    UPDATE devices SET updated_at = datetime('now','localtime')
    WHERE serial = NEW.serial;
END;
"""

_MIGRATIONS = [
    "ALTER TABLE devices ADD COLUMN persona_messenger TEXT DEFAULT ''",
    "ALTER TABLE devices ADD COLUMN persona_line TEXT DEFAULT ''",
    "ALTER TABLE devices ADD COLUMN persona_whatsapp TEXT DEFAULT ''",
]


class DeviceRegistryDB:
    def __init__(self, db_path: str | Path = ""):
        self._db = Path(db_path) if db_path else _DEFAULT_DB
        self._lock = threading.Lock()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        self._db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_schema(self):
        with self._lock:
            with self._connect() as conn:
                conn.executescript(_DDL + _TRIGGER)
                for sql in _MIGRATIONS:
                    try:
                        conn.execute(sql)
                    except sqlite3.OperationalError:
                        pass  # column already exists

    # ── Write ────────────────────────────────────────────────────────────

    def upsert(self, serial: str, **fields) -> Dict:
        """
        插入或更新设备信息。仅更新传入的字段。
        首次插入时自动生成 alias（"{number:02d}号"）和 location（"{group_name}-手机{number:02d}"）。
        """
        if not serial:
            raise ValueError("serial 不能为空")

        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM devices WHERE serial=?", (serial,)
                ).fetchone()

                if row is None:
                    # 首次插入
                    number = int(fields.get("number", 0))
                    group = fields.get("group_name", "主控")
                    alias = fields.get("alias") or (f"{number:02d}号" if number else "")
                    location = fields.get("location") or (
                        f"{group}-手机{number:02d}" if number else ""
                    )
                    conn.execute("""
                        INSERT INTO devices
                        (serial, hw_serial, android_id, model, number, label,
                         alias, location, group_name, wifi_ip,
                         platform_messenger, platform_line, platform_whatsapp)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (
                        serial,
                        fields.get("hw_serial", ""),
                        fields.get("android_id", ""),
                        fields.get("model", ""),
                        number,
                        fields.get("label", ""),
                        alias,
                        location,
                        group,
                        fields.get("wifi_ip", ""),
                        fields.get("platform_messenger", ""),
                        fields.get("platform_line", ""),
                        fields.get("platform_whatsapp", ""),
                    ))
                    log.info("[DeviceRegistry] 注册新设备 serial=%s number=%s location=%s",
                             serial[:8], number, location)
                else:
                    # 仅更新传入字段
                    allowed = {
                        "hw_serial", "android_id", "model", "number", "label",
                        "alias", "location", "group_name", "wifi_ip",
                        "platform_messenger", "platform_line", "platform_whatsapp",
                        "persona_messenger", "persona_line", "persona_whatsapp",
                        "wallpaper_hash", "wallpaper_set_at",
                    }
                    updates = {k: v for k, v in fields.items() if k in allowed}
                    # 自动补 alias / location（若 number 或 group_name 变了）
                    if "number" in updates or "group_name" in updates:
                        new_num = int(updates.get("number", row["number"] or 0))
                        new_grp = updates.get("group_name", row["group_name"] or "主控")
                        if "alias" not in updates:
                            updates["alias"] = f"{new_num:02d}号"
                        if "location" not in updates:
                            updates["location"] = f"{new_grp}-手机{new_num:02d}"
                    if updates:
                        set_sql = ", ".join(f"{k}=?" for k in updates)
                        conn.execute(
                            f"UPDATE devices SET {set_sql} WHERE serial=?",
                            list(updates.values()) + [serial],
                        )

        return self.get(serial) or {}

    def mark_wallpaper(self, serial: str, wallpaper_hash: str):
        """记录壁纸部署成功。"""
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE devices SET wallpaper_hash=?, wallpaper_set_at=? WHERE serial=?",
                    (wallpaper_hash, time.time(), serial),
                )

    # ── Read ─────────────────────────────────────────────────────────────

    def get(self, serial: str) -> Optional[Dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM devices WHERE serial=?", (serial,)
            ).fetchone()
            return dict(row) if row else None

    def get_by_number(self, number: int) -> Optional[Dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM devices WHERE number=?", (number,)
            ).fetchone()
            return dict(row) if row else None

    def get_by_label(self, label: str) -> Optional[Dict]:
        """按简码（IJ8/Q4N/VWN/XW8）查询。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM devices WHERE label=?", (label.upper(),)
            ).fetchone()
            return dict(row) if row else None

    def resolve(self, identifier: str) -> Optional[Dict]:
        """
        通用查询：接受 serial / 编号数字 / 标签 / location / alias。
        方便 AI 和人类用各种形式引用手机。
        """
        # 1. 直接 serial
        r = self.get(identifier)
        if r:
            return r
        # 2. 纯数字 → number
        if identifier.isdigit():
            r = self.get_by_number(int(identifier))
            if r:
                return r
        # 3. 简码（IJ8 等）
        r = self.get_by_label(identifier)
        if r:
            return r
        # 4. location / alias 模糊匹配
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM devices WHERE location LIKE ? OR alias LIKE ? OR label LIKE ?",
                (f"%{identifier}%", f"%{identifier}%", f"%{identifier}%"),
            ).fetchall()
            if rows:
                return dict(rows[0])
        return None

    def get_by_group(self, group_name: str) -> List[Dict]:
        """按分组名查询（主控/W03/W175）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM devices WHERE group_name=? ORDER BY number",
                (group_name,),
            ).fetchall()
            return [dict(r) for r in rows]

    def all(self) -> List[Dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM devices ORDER BY number"
            ).fetchall()
            return [dict(r) for r in rows]

    def summary(self) -> str:
        """返回人机交互友好的设备清单字符串。"""
        devices = self.all()
        if not devices:
            return "（设备注册表为空）"
        lines = ["编号  位置标签        简码  Serial前8位  IP              平台"]
        lines.append("-" * 65)
        for d in devices:
            platforms = "+".join(filter(None, [
                "MSG" if d.get("platform_messenger") else "",
                "LINE" if d.get("platform_line") else "",
                "WA" if d.get("platform_whatsapp") else "",
            ])) or "-"
            lines.append(
                f"{d['number']:>3}号  {d['location']:<14}  {d['label']:<4}  "
                f"{d['serial'][:8]:<12}  {d['wifi_ip']:<16}  {platforms}"
            )
        return "\n".join(lines)


# ── 单例 ──────────────────────────────────────────────────────────────────

_instance: Optional[DeviceRegistryDB] = None
_inst_lock = threading.Lock()


def get_device_registry(db_path: str | Path = "") -> DeviceRegistryDB:
    global _instance
    if _instance is None:
        with _inst_lock:
            if _instance is None:
                _instance = DeviceRegistryDB(db_path)
    return _instance
