"""自研浏览器/设备指纹（M4）。

「一号一指纹」是多账号防关联的另一核心。本模块自研生成一套稳定、可复现的指纹画像
（不依赖第三方指纹浏览器）：给定 ``seed``（通常用账号 id / 备注）确定性派生 UA、屏幕、
时区、语言、WebGL、Canvas 噪声种子、硬件并发数等，供 web 模式的隔离浏览器注入，或
protocol 模式映射为 device_model / app_version 等设备标识。

- ``generate_fingerprint(seed)``：纯函数，确定性（同 seed → 同指纹），可单测。
- ``FingerprintStore``：持久化已生成指纹（独立 SQLite），登录时绑定 fingerprint_id。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# 指纹素材池（自研，覆盖主流真机/桌面组合，避免明显异常值触发风控）
_UA_POOL = [
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/124.0.0.0 Safari/537.36", "Win32", "Windows"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/124.0.0.0 Safari/537.36", "MacIntel", "macOS"),
    ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/124.0.0.0 Safari/537.36", "Linux x86_64", "Linux"),
    ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
     "Version/17.4 Mobile/15E148 Safari/604.1", "iPhone", "iOS"),
    ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/124.0.0.0 Mobile Safari/537.36", "Linux armv8l", "Android"),
]
_SCREENS = [(1920, 1080), (1536, 864), (1440, 900), (1366, 768), (2560, 1440), (390, 844), (412, 915)]
_TIMEZONES = [
    "Asia/Shanghai", "Asia/Hong_Kong", "Asia/Singapore", "Asia/Tokyo",
    "Asia/Manila", "Asia/Bangkok", "America/New_York", "Europe/London",
]
_LANGS = [
    ["zh-CN", "zh", "en"], ["en-US", "en"], ["zh-TW", "zh", "en"],
    ["en-GB", "en"], ["ja-JP", "ja", "en"],
]
_WEBGL_VENDORS = [
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Apple", "Apple GPU"),
    ("Qualcomm", "Adreno (TM) 740"),
]
_HW_CONCURRENCY = [4, 6, 8, 12, 16]
_DEVICE_MEMORY = [4, 8, 16, 32]


def _pick(values: List[Any], digest: bytes, offset: int) -> Any:
    idx = digest[offset % len(digest)] % len(values)
    return values[idx]


def generate_fingerprint(seed: Optional[str] = None) -> Dict[str, Any]:
    """确定性生成一套指纹画像。``seed`` 为空则随机。"""
    seed = seed or secrets.token_hex(8)
    digest = hashlib.sha256(str(seed).encode("utf-8")).digest()
    ua, platform, os_name = _pick(_UA_POOL, digest, 0)
    width, height = _pick(_SCREENS, digest, 1)
    timezone = _pick(_TIMEZONES, digest, 2)
    languages = _pick(_LANGS, digest, 3)
    webgl_vendor, webgl_renderer = _pick(_WEBGL_VENDORS, digest, 4)
    return {
        "seed": str(seed),
        "user_agent": ua,
        "platform": platform,
        "os": os_name,
        "screen": {"width": width, "height": height, "pixel_ratio":
                   2 if os_name in ("iOS", "Android", "macOS") else 1},
        "timezone": timezone,
        "languages": languages,
        "language": languages[0],
        "webgl_vendor": webgl_vendor,
        "webgl_renderer": webgl_renderer,
        "hardware_concurrency": _pick(_HW_CONCURRENCY, digest, 5),
        "device_memory": _pick(_DEVICE_MEMORY, digest, 6),
        # Canvas/Audio 噪声种子：注入时按此种子施加微扰，使每号 hash 稳定且互不相同
        "canvas_noise_seed": digest[:8].hex(),
        "audio_noise_seed": digest[8:16].hex(),
    }


def summarize(profile: Dict[str, Any]) -> str:
    """一行人类可读摘要（UI 展示用）。"""
    if not profile:
        return ""
    scr = profile.get("screen") or {}
    return (f"{profile.get('os', '?')} · {profile.get('language', '?')} · "
            f"{scr.get('width', '?')}×{scr.get('height', '?')} · "
            f"{profile.get('timezone', '?')} · {profile.get('webgl_vendor', '?')}")


_DDL = """
CREATE TABLE IF NOT EXISTS fingerprints (
    fingerprint_id  TEXT PRIMARY KEY,
    seed            TEXT NOT NULL DEFAULT '',
    profile_json    TEXT NOT NULL DEFAULT '{}',
    label           TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL DEFAULT 0
);
"""


class FingerprintStore:
    """已生成指纹的持久化（线程安全 SQLite 封装）。"""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._db_path), check_same_thread=False, timeout=10
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_DDL)
            self._conn.commit()

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        try:
            d["profile"] = json.loads(d.pop("profile_json", "{}") or "{}")
        except (json.JSONDecodeError, TypeError):
            d["profile"] = {}
        return d

    def create(self, seed: Optional[str] = None, label: str = "") -> Dict[str, Any]:
        profile = generate_fingerprint(seed)
        fid = "fp_" + secrets.token_hex(5)
        with self._lock:
            self._conn.execute(
                """INSERT INTO fingerprints (fingerprint_id, seed, profile_json, label, created_at)
                   VALUES (?,?,?,?,?)""",
                (fid, profile["seed"], json.dumps(profile, ensure_ascii=False),
                 label, time.time()),
            )
            self._conn.commit()
        return {"fingerprint_id": fid, "profile": profile, "label": label}

    def get(self, fingerprint_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM fingerprints WHERE fingerprint_id=?", (fingerprint_id,)
            ).fetchone()
        return self._row(row) if row else None

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM fingerprints ORDER BY created_at"
            ).fetchall()
        return [self._row(r) for r in rows]


_store: Optional[FingerprintStore] = None
_store_lock = threading.Lock()


def get_fingerprint_store(db_path: Optional[Path] = None) -> FingerprintStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                path = Path(db_path) if db_path else Path("config/fingerprints.db")
                _store = FingerprintStore(path)
    return _store
