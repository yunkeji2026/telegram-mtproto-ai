"""P1-4：坐席出向翻译漏斗观测（进程级单例）。

区别于 ``translation_engine_stats``（引擎层：每个引擎的调用/成功/延迟），本模块统计
**工作台出向发送漏斗**：坐席每次发送中翻译的请求/命中/跳过/失败，以及 ``auto``
（自动客户语言）解析的成败、引擎降级次数、各目标语译出分布。

用途：
- 工程：auto 解析失败率（语言检测缺口）、翻译失败/降级率（引擎健康）。
- 市场：跨语言翻译覆盖率、按客户语言的服务分布（喂经理看板 ROI）。

风格对齐 src/ai/translation_engine_stats.py 与 provider_stats.py：无新增依赖；
JSON 供 /api/workspace/metrics，Prometheus 文本由路由读 dump_prom() 拼接。
绝不记录任何原文/译文，只存计数与目标语种码。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional


class OutboundTranslationStats:
    """出向翻译漏斗计数（线程安全，进程级）。"""

    __slots__ = (
        "_lock", "_started_at", "_last_ts",
        "sends_total", "requested", "translated", "skipped", "failed",
        "auto_requested", "auto_resolved", "auto_unresolved", "degraded",
        "_by_lang",
    )

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._started_at = time.time()
        self._last_ts = 0.0
        self.sends_total = 0
        self.requested = 0       # 请求翻译（target 非空且未 skip）的发送数
        self.translated = 0      # 实际译出（实发文本 != 原文）的发送数
        self.skipped = 0         # 请求了但未译（target 空/同语种/skip）的发送数
        self.failed = 0          # 翻译过程异常/失败的发送数
        self.auto_requested = 0  # target_lang="auto" 的发送数
        self.auto_resolved = 0   # auto 成功解析出客户语言
        self.auto_unresolved = 0 # auto 未能解析（客户语言 unknown）
        self.degraded = 0        # 译文由降级/不可用引擎产出（provider none/identity 或带 error）
        self._by_lang: Dict[str, int] = {}  # 各目标语译出次数

    def record_send(
        self,
        *,
        requested: bool,
        is_auto: bool = False,
        auto_resolved: Optional[bool] = None,
        translated: bool = False,
        target_lang: str = "",
        degraded: bool = False,
        failed: bool = False,
    ) -> None:
        """记录一次（已投递成功的）出向发送的翻译漏斗结果。

        requested：本次发送是否请求了翻译（target 非空且未 skip）。
        is_auto：target_lang 是否为 "auto"；auto_resolved：auto 是否成功解析（True/False/None）。
        translated：是否实际译出（实发 != 原文）；target_lang：实际目标语（译出时）。
        degraded：译文是否来自降级/不可用引擎；failed：翻译是否异常失败。
        """
        with self._lock:
            self.sends_total += 1
            self._last_ts = time.time()
            if requested:
                self.requested += 1
            if is_auto:
                self.auto_requested += 1
                if auto_resolved is True:
                    self.auto_resolved += 1
                elif auto_resolved is False:
                    self.auto_unresolved += 1
            if failed:
                self.failed += 1
            elif translated:
                self.translated += 1
                lang = str(target_lang or "").strip() or "unknown"
                self._by_lang[lang] = self._by_lang.get(lang, 0) + 1
                if degraded:
                    self.degraded += 1
            elif requested:
                self.skipped += 1

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            cov = round(self.translated / self.sends_total, 4) if self.sends_total else 0
            auto_fail = (
                round(self.auto_unresolved / self.auto_requested, 4)
                if self.auto_requested else 0
            )
            return {
                "started_at": self._started_at,
                "last_record_ts": self._last_ts,
                "sends_total": self.sends_total,
                "requested": self.requested,
                "translated": self.translated,
                "skipped": self.skipped,
                "failed": self.failed,
                "auto_requested": self.auto_requested,
                "auto_resolved": self.auto_resolved,
                "auto_unresolved": self.auto_unresolved,
                "degraded": self.degraded,
                "coverage_rate": cov,             # 译出 / 总发送
                "auto_unresolved_rate": auto_fail,
                "by_target_lang": dict(sorted(self._by_lang.items())),
            }

    def dump_prom(self) -> str:
        with self._lock:
            lines = [
                "# HELP outbound_xlate_sends_total Agent outbound sends (denominator)",
                "# TYPE outbound_xlate_sends_total counter",
                f"outbound_xlate_sends_total {self.sends_total}",
                "# HELP outbound_xlate_translated_total Outbound sends actually translated",
                "# TYPE outbound_xlate_translated_total counter",
                f"outbound_xlate_translated_total {self.translated}",
                "# HELP outbound_xlate_skipped_total Outbound translation requested but no-op",
                "# TYPE outbound_xlate_skipped_total counter",
                f"outbound_xlate_skipped_total {self.skipped}",
                "# HELP outbound_xlate_failed_total Outbound translation failures",
                "# TYPE outbound_xlate_failed_total counter",
                f"outbound_xlate_failed_total {self.failed}",
                "# HELP outbound_xlate_auto_unresolved_total Auto target-lang unresolved",
                "# TYPE outbound_xlate_auto_unresolved_total counter",
                f"outbound_xlate_auto_unresolved_total {self.auto_unresolved}",
                "# HELP outbound_xlate_degraded_total Outbound translations via degraded engine",
                "# TYPE outbound_xlate_degraded_total counter",
                f"outbound_xlate_degraded_total {self.degraded}",
                "# HELP outbound_xlate_by_lang_total Translated outbound sends by target lang",
                "# TYPE outbound_xlate_by_lang_total counter",
            ]
            for lang, n in sorted(self._by_lang.items()):
                lines.append(f'outbound_xlate_by_lang_total{{lang="{_esc(lang)}"}} {int(n)}')
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self.sends_total = 0
            self.requested = 0
            self.translated = 0
            self.skipped = 0
            self.failed = 0
            self.auto_requested = 0
            self.auto_resolved = 0
            self.auto_unresolved = 0
            self.degraded = 0
            self._by_lang.clear()
            self._last_ts = 0.0


def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


_SINGLETON: Optional[OutboundTranslationStats] = None
_LOCK = threading.Lock()


def get_outbound_translation_stats() -> OutboundTranslationStats:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = OutboundTranslationStats()
    return _SINGLETON


__all__ = ["OutboundTranslationStats", "get_outbound_translation_stats"]
