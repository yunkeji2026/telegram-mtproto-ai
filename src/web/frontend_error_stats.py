"""前端「哑按钮」运行时错误观测（进程级单例）。

背景：内联 `on*="fn()"` 引用了未挂 window 的函数（IIFE 内漏挂 / 拼写错 / 动态点属性拼接
被当减法），点击抛 `ReferenceError` 静默失效。静态门禁（test_*_inline_handlers_*、
test_template_dynamic_dot_access）挡「入库前」，运行时兜底守卫（unified_inbox +
_rpa_shared_scripts）弹红条给用户看——但**后台此前无感知**：哪页哪函数点崩、多频，全靠用户上报。

本模块把这些前端错误变成**可观测计数**：dead-click 守卫捕获后 beacon 到
`POST /api/telemetry/frontend-error`，此处按 (page, fn, type) 累计，经 dump()→
`/api/workspace/metrics.frontend_errors`、dump_prom()→Prometheus，闭合「测不到→线上也能被发现」。

风格对齐 src/ai/outbound_translation_stats.py：无新增依赖，线程安全，进程级单例。
**只存计数 + 已消毒的 page 路径 + 标识符名**，绝不存完整报文/URL 查询串/堆栈（防 PII/敏感串泄漏）。
distinct key 有上限（防脏数据/刷量把内存撑爆），超限归入 `__other__` 并计 overflow。
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Dict, Optional

_MAX_KEYS = 100  # by_page / by_fn 各自最多保留的 distinct key 数
_IDENT_RE = re.compile(r"^[A-Za-z_$][\w$]*$")
# page 路径只保留合法 URL path 字符（丢查询串/hash/异常内容），截断防超长
_PATH_SAFE = re.compile(r"[^A-Za-z0-9/_\-.:]")
_KNOWN_TYPES = {"ReferenceError", "TypeError", "SyntaxError", "RangeError", "Error"}


def _san_page(page: str) -> str:
    p = str(page or "").split("?", 1)[0].split("#", 1)[0].strip()
    p = _PATH_SAFE.sub("", p)
    if not p:
        return "unknown"
    return p[:80]


def _san_fn(fn: str) -> str:
    f = str(fn or "").strip()
    return f[:64] if _IDENT_RE.match(f) else "unknown"


def _san_type(etype: str) -> str:
    t = str(etype or "").strip()
    return t if t in _KNOWN_TYPES else "Error"


class FrontendErrorStats:
    """前端运行时错误计数（线程安全，进程级）。"""

    __slots__ = (
        "_lock", "_started_at", "_last_ts",
        "total", "overflow", "_by_fn", "_by_page", "_by_type",
    )

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._started_at = time.time()
        self._last_ts = 0.0
        self.total = 0
        self.overflow = 0                     # distinct key 超限被归入 __other__ 的次数
        self._by_fn: Dict[str, int] = {}
        self._by_page: Dict[str, int] = {}
        self._by_type: Dict[str, int] = {}

    @staticmethod
    def _bump(d: Dict[str, int], key: str) -> bool:
        """key 已存在或未超限 → +1 返回 False；超限 → 归 __other__ 返回 True（overflow）。"""
        if key in d or len(d) < _MAX_KEYS:
            d[key] = d.get(key, 0) + 1
            return False
        d["__other__"] = d.get("__other__", 0) + 1
        return True

    def record(self, *, page: str = "", fn: str = "", etype: str = "") -> None:
        p, f, t = _san_page(page), _san_fn(fn), _san_type(etype)
        with self._lock:
            self.total += 1
            self._last_ts = time.time()
            of = self._bump(self._by_page, p)
            of = self._bump(self._by_fn, f) or of
            self._by_type[t] = self._by_type.get(t, 0) + 1  # type 是小枚举，不设上限
            if of:
                self.overflow += 1

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "started_at": self._started_at,
                "last_record_ts": self._last_ts,
                "total": self.total,
                "overflow": self.overflow,
                "by_type": dict(sorted(self._by_type.items())),
                "by_fn": dict(sorted(self._by_fn.items(), key=lambda kv: (-kv[1], kv[0]))),
                "by_page": dict(sorted(self._by_page.items(), key=lambda kv: (-kv[1], kv[0]))),
            }

    def dump_prom(self) -> str:
        with self._lock:
            lines = [
                "# HELP frontend_errors_total Frontend dead-click runtime errors (denominator)",
                "# TYPE frontend_errors_total counter",
                f"frontend_errors_total {self.total}",
                "# HELP frontend_errors_by_type_total Frontend runtime errors by JS error type",
                "# TYPE frontend_errors_by_type_total counter",
            ]
            for t, n in sorted(self._by_type.items()):
                lines.append(f'frontend_errors_by_type_total{{type="{_esc(t)}"}} {int(n)}')
            lines += [
                "# HELP frontend_errors_by_page_total Frontend runtime errors by page path",
                "# TYPE frontend_errors_by_page_total counter",
            ]
            for p, n in sorted(self._by_page.items()):
                lines.append(f'frontend_errors_by_page_total{{page="{_esc(p)}"}} {int(n)}')
            lines += [
                "# HELP frontend_errors_by_fn_total Frontend runtime errors by referenced symbol",
                "# TYPE frontend_errors_by_fn_total counter",
            ]
            for f, n in sorted(self._by_fn.items()):
                lines.append(f'frontend_errors_by_fn_total{{fn="{_esc(f)}"}} {int(n)}')
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self.total = 0
            self.overflow = 0
            self._by_fn.clear()
            self._by_page.clear()
            self._by_type.clear()
            self._last_ts = 0.0


def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


_SINGLETON: Optional[FrontendErrorStats] = None
_LOCK = threading.Lock()


def get_frontend_error_stats() -> FrontendErrorStats:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = FrontendErrorStats()
    return _SINGLETON


__all__ = ["FrontendErrorStats", "get_frontend_error_stats"]
