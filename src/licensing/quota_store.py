"""P0-4 免费试用：翻译/TTS「字符额度」计量 + 强制（SQLite 持久化，仿 tts_cost_store）。

定位
====
license payload 的 ``included_chars``（C2）只定义「含多少」；本模块负责「用了多少」：
- **按日 (lic_id, category) 增量 upsert** 落 ``config/license_quota.db``——跨重启累计，
  行数上界 = 天数 × 类目数 × 授权数（极小），故**不做 prune**（额度以历史累计为准，
  裁剪等于送额度）。
- **治理随授权走，零配置**：仅当当前授权 ``licensed`` 且 ``included_chars > 0`` 时才建库/
  记账；社区模式 / 无额度授权部署恒零 IO（延续「新子系统默认关」精神，开关即授权本身）。
- **默认不阻断**：``check_license_quota`` 只有在 ``licensing.enforce=true``（C0-3 同一开关）
  且额度用尽时才判「不放行」；enforce 关时仅 warn 一次（对齐 message_quota「软状态」语义）。
- 判定用 ``gate.quota_exceeded`` 纯函数；本模块负责 IO 与装配。

接线点（C1/C3/C12）：
- 翻译：``TranslationService.translate`` 引擎调用前 check、成功后 record（缓存命中不计）。
- TTS：``TTSPipeline.synthesize`` 合成前 check、成功后 record（缓存命中不计）——覆盖
  autosend / 原生 voice_reply / 手动坐席 / send-voice / tts-test 全部链路（C12 顺带闭环）。
被阻断的调用返回稳定错误码 ``license_quota_exceeded``，路由层据此出 i18n 文案。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 稳定错误码：翻译/TTS 被额度阻断时透传给上层（路由映射 i18n 文案）。
QUOTA_EXCEEDED_ERROR = "license_quota_exceeded"

_DDL = """
CREATE TABLE IF NOT EXISTS license_char_usage (
    lic_id    TEXT NOT NULL,
    day       TEXT NOT NULL,
    category  TEXT NOT NULL,
    chars     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (lic_id, day, category)
);
"""


def _day_str(now: Optional[float] = None) -> str:
    """UTC 日期键 ``YYYY-MM-DD``（与 tts_cost_store 同口径，跨时区部署一致）。"""
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


class LicenseQuotaStore:
    """按 (lic_id, 日, 类目) 聚合的字符用量（线程安全 SQLite）。"""

    def __init__(self, db_path: Any = ":memory:") -> None:
        self._is_mem = str(db_path) == ":memory:"
        if not self._is_mem:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, timeout=10,
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            if not self._is_mem:
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_DDL)
            self._conn.commit()

    def record(
        self, lic_id: str, category: str, chars: int, *, now: Optional[float] = None,
    ) -> None:
        """记一笔已交付字符到当日聚合。绝不抛（吞掉所有异常）。"""
        n = int(chars or 0)
        if n <= 0:
            return
        day = _day_str(now)
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO license_char_usage (lic_id, day, category, chars) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(lic_id, day, category) DO UPDATE SET "
                    "  chars = chars + excluded.chars",
                    (str(lic_id or "default"), day, str(category or "other"), n),
                )
                self._conn.commit()
        except Exception:
            logger.debug("[license_quota] record 失败（已忽略）", exc_info=True)

    def used_chars(self, lic_id: str, category: Optional[str] = None) -> int:
        """该授权历史累计已用字符（可按类目过滤）。读失败按 0（不误伤放行）。"""
        try:
            with self._lock:
                if category:
                    row = self._conn.execute(
                        "SELECT COALESCE(SUM(chars), 0) AS s FROM license_char_usage "
                        "WHERE lic_id = ? AND category = ?",
                        (str(lic_id or "default"), str(category)),
                    ).fetchone()
                else:
                    row = self._conn.execute(
                        "SELECT COALESCE(SUM(chars), 0) AS s FROM license_char_usage "
                        "WHERE lic_id = ?",
                        (str(lic_id or "default"),),
                    ).fetchone()
            return int(row["s"] or 0)
        except Exception:
            logger.debug("[license_quota] used_chars 读取失败（按 0）", exc_info=True)
            return 0

    def usage(self, lic_id: str) -> Dict[str, Any]:
        """观测快照：{total, by_category, today}。读失败返回零值。"""
        out: Dict[str, Any] = {"total": 0, "by_category": {}, "today": 0}
        try:
            today = _day_str()
            with self._lock:
                for r in self._conn.execute(
                    "SELECT category, SUM(chars) AS s FROM license_char_usage "
                    "WHERE lic_id = ? GROUP BY category",
                    (str(lic_id or "default"),),
                ).fetchall():
                    out["by_category"][str(r["category"])] = int(r["s"] or 0)
                row = self._conn.execute(
                    "SELECT COALESCE(SUM(chars), 0) AS s FROM license_char_usage "
                    "WHERE lic_id = ? AND day = ?",
                    (str(lic_id or "default"), today),
                ).fetchone()
                out["today"] = int(row["s"] or 0)
            out["total"] = sum(out["by_category"].values())
        except Exception:
            logger.debug("[license_quota] usage 读取失败（返回零值）", exc_info=True)
        return out

    def remaining(self, lic_id: str, included_chars: int) -> Optional[int]:
        """剩余额度；``included_chars<=0``（不限）返回 None。下限截断为 0。"""
        inc = int(included_chars or 0)
        if inc <= 0:
            return None
        return max(0, inc - self.used_chars(lic_id))


# ── 模块级单例 + 惰性建库（治理随授权走）────────────────────────────────────
_STORE: Optional[LicenseQuotaStore] = None
_DB_PATH: Optional[str] = None
_CFG_LOCK = threading.Lock()
# enforce 关时「额度已尽」只 warn 一次/进程/授权（防刷屏；对齐 message_quota 软语义）
_WARNED_LIC_IDS: set = set()


def _default_db_path() -> str:
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(here, "config", "license_quota.db")


def configure_license_quota_store(
    *, db_path: Any = None, store: Optional[LicenseQuotaStore] = None,
) -> Optional[LicenseQuotaStore]:
    """启动期装配（可选）：覆盖 db 路径或直接注入 store（测试用）。幂等。"""
    global _STORE, _DB_PATH
    with _CFG_LOCK:
        if store is not None:
            _STORE = store
        if db_path is not None:
            _DB_PATH = str(db_path)
        return _STORE


def get_license_quota_store() -> Optional[LicenseQuotaStore]:
    """当前 store（未建库 → None）。供只读观测端点用。"""
    return _STORE


def reset_license_quota_store() -> None:
    """测试钩子：清空单例/路径/告警去重。"""
    global _STORE, _DB_PATH
    with _CFG_LOCK:
        _STORE = None
        _DB_PATH = None
        _WARNED_LIC_IDS.clear()


def _ensure_store() -> Optional[LicenseQuotaStore]:
    """惰性建库（仅在确有额度授权时被调用）。建库失败降级为不计量，绝不抛。"""
    global _STORE
    with _CFG_LOCK:
        if _STORE is None:
            try:
                _STORE = LicenseQuotaStore(_DB_PATH or _default_db_path())
            except Exception:
                logger.warning("[license_quota] 建库失败，字符额度计量禁用", exc_info=True)
                _STORE = None
        return _STORE


def _current_status():
    from src.licensing.license_manager import get_license_manager

    return get_license_manager().status()


def record_license_chars(
    category: str, chars: int, *, lic_status: Any = None,
) -> None:
    """成功交付后记账（翻译/TTS 热路旁路调用一行）。

    无额度授权（unlicensed / included_chars<=0）→ 立即返回零 IO。绝不抛。
    """
    try:
        n = int(chars or 0)
        if n <= 0:
            return
        st = lic_status if lic_status is not None else _current_status()
        if not getattr(st, "licensed", False):
            return
        if int(getattr(st, "included_chars", 0) or 0) <= 0:
            return
        store = _ensure_store()
        if store is None:
            return
        store.record(str(getattr(st, "lic_id", "") or "default"), category, n)
    except Exception:
        logger.debug("[license_quota] record_license_chars 失败（已忽略）", exc_info=True)


def check_license_quota(*, lic_status: Any = None) -> Dict[str, Any]:
    """额度闸门（C3）：返回 {allowed, exceeded, enforce, used, included, remaining, lic_id}。

    - 无额度授权 → allowed=True（不限）；
    - 额度用尽 + ``enforce=True`` → allowed=False（调用方应以
      ``QUOTA_EXCEEDED_ERROR`` 阻断该次翻译/合成，**不阻断消息投递本身**）；
    - 额度用尽 + enforce 关 → allowed=True，仅 warn 一次/进程（软配额，对齐 message_quota）。
    检查自身任何异常 → 放行（额度闸门绝不把主流程打挂）。
    """
    out: Dict[str, Any] = {
        "allowed": True, "exceeded": False, "enforce": False,
        "used": 0, "included": 0, "remaining": None, "lic_id": "",
    }
    try:
        st = lic_status if lic_status is not None else _current_status()
        included = int(getattr(st, "included_chars", 0) or 0)
        out["enforce"] = bool(getattr(st, "enforce", False))
        out["included"] = included
        out["lic_id"] = str(getattr(st, "lic_id", "") or "default")
        if not getattr(st, "licensed", False) or included <= 0:
            return out
        store = _ensure_store()
        if store is None:
            return out
        used = store.used_chars(out["lic_id"])
        out["used"] = used
        out["remaining"] = max(0, included - used)
        from src.licensing.gate import quota_exceeded

        out["exceeded"] = quota_exceeded(getattr(st, "state", ""), used, included)
        if out["exceeded"]:
            if out["enforce"]:
                out["allowed"] = False
            elif out["lic_id"] not in _WARNED_LIC_IDS:
                _WARNED_LIC_IDS.add(out["lic_id"])
                logger.warning(
                    "[license_quota] 授权 %s 字符额度已用尽（%s/%s）；"
                    "licensing.enforce 未开启，仅提醒不阻断",
                    out["lic_id"], used, included,
                )
    except Exception:
        logger.debug("[license_quota] check 失败（放行）", exc_info=True)
    return out


__all__ = [
    "QUOTA_EXCEEDED_ERROR",
    "LicenseQuotaStore",
    "check_license_quota",
    "configure_license_quota_store",
    "get_license_quota_store",
    "record_license_chars",
    "reset_license_quota_store",
]
