"""实时共情语音通话观测（进程级单例）。

给 realtime_voice WS 网关埋点，回答运营三问——「有人打进来吗（发起/接通率）、通得顺吗
（挂断原因/时长/主机健康）、显存开关折腾几次（load/unload）」。JSON 供
``/api/workspace/metrics``，Prometheus 文本由 Web 路由读 ``dump_prom()`` 拼接。

**绝不记录任何音频/转写/文本原文**，只记计数与时长。best-effort：任何异常都吞掉，
绝不阻塞通话主链路。风格对齐 ``src/ai/voice_synth_stats.py`` / ``translation_engine_stats.py``：
进程级单例、无新增依赖、``dump``/``dump_prom``/``reset``。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

# 结束原因白名单（防维度爆炸 / 标签注入）；其余归 "other"。
_END_REASONS = (
    "normal",            # 正常挂断（浏览器断开）
    "relay_error",       # 中继异常结束
    "unauthorized",      # 握手口令不符
    "host_unreachable",  # 语音主机健康探测失败
    "connect_failed",    # 连主机失败
    "hello_error",       # 开场握手解析失败
    "other",
)


class RealtimeVoiceStats:
    """实时语音通话计数聚合（线程安全，进程级）。"""

    __slots__ = (
        "_lock", "_attempts", "_connected", "_by_reason", "_active", "_peak_active",
        "_dur_total", "_dur_count", "_dur_max", "_last_dur",
        "_health_ok", "_health_fail", "_engine_load", "_engine_unload",
        "_started_at", "_last_call_ts", "_last_end_ts", "_last_reason",
    )

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._attempts = 0                      # 进入（enabled 后）的通话尝试数
        self._connected = 0                     # 成功接通（session 建立、EV_READY 已发）数
        self._by_reason: Dict[str, int] = {}    # 结束原因分布
        self._active = 0                        # 当前进行中的已接通通话
        self._peak_active = 0                   # 并发峰值
        self._dur_total = 0.0                   # 已接通通话总时长（秒）
        self._dur_count = 0                     # 计入时长的通话数
        self._dur_max = 0.0
        self._last_dur = 0.0
        self._health_ok = 0                     # 主机健康探测成功次数
        self._health_fail = 0                   # 主机健康探测失败次数
        self._engine_load = 0                   # 显存「启动引擎」调用次数
        self._engine_unload = 0                 # 显存「释放显存」调用次数
        self._started_at = time.time()
        self._last_call_ts = 0.0
        self._last_end_ts = 0.0
        self._last_reason = ""

    def attempt(self) -> None:
        """一次通话尝试开始（已过 enabled 闸）。"""
        with self._lock:
            self._attempts += 1
            self._last_call_ts = time.time()
        try:
            from src.ai.realtime_voice_trend_store import record_realtime_voice_trend
            record_realtime_voice_trend(attempts=1)
        except Exception:
            pass

    def connected(self) -> None:
        """通话成功接通（session 建立）——进行中 +1，刷新并发峰值。"""
        with self._lock:
            self._connected += 1
            self._active += 1
            if self._active > self._peak_active:
                self._peak_active = self._active
        try:
            from src.ai.realtime_voice_trend_store import record_realtime_voice_trend
            record_realtime_voice_trend(connected=1)
        except Exception:
            pass

    def ended(self, reason: str = "normal", *, was_connected: bool = False,
              duration_sec: float = 0.0) -> None:
        """一次通话结束。``was_connected`` 时把进行中 -1 并计入时长（仅正数）。"""
        r = reason if reason in _END_REASONS else "other"
        with self._lock:
            self._by_reason[r] = self._by_reason.get(r, 0) + 1
            self._last_reason = r
            self._last_end_ts = time.time()
            if was_connected:
                self._active = max(0, self._active - 1)
                d = float(duration_sec or 0.0)
                if d > 0:
                    self._dur_total += d
                    self._dur_count += 1
                    self._last_dur = d
                    if d > self._dur_max:
                        self._dur_max = d
        try:
            from src.ai.realtime_voice_trend_store import record_realtime_voice_trend
            kw: Dict[str, int] = {}
            if r == "host_unreachable":
                kw["host_unreachable"] = 1
            elif r == "connect_failed":
                kw["connect_failed"] = 1
            if kw:
                record_realtime_voice_trend(**kw)
        except Exception:
            pass

    def health_probe(self, ok: bool) -> None:
        """记一次语音主机健康探测结果。"""
        with self._lock:
            if ok:
                self._health_ok += 1
            else:
                self._health_fail += 1
        try:
            from src.ai.realtime_voice_trend_store import record_realtime_voice_trend
            if ok:
                record_realtime_voice_trend(health_ok=1)
            else:
                record_realtime_voice_trend(health_fail=1)
        except Exception:
            pass

    def engine_action(self, action: str) -> None:
        """记一次显存生命周期动作（load=启动引擎 / unload=释放显存）。"""
        with self._lock:
            if action == "load":
                self._engine_load += 1
            elif action == "unload":
                self._engine_unload += 1

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            att = self._attempts
            hp = self._health_ok + self._health_fail
            return {
                "started_at": self._started_at,
                "last_call_ts": self._last_call_ts,
                "last_end_ts": self._last_end_ts,
                "attempts": int(att),
                "connected": int(self._connected),
                "connect_rate": round(self._connected / att, 4) if att else 0,
                "active": int(self._active),
                "peak_active": int(self._peak_active),
                "avg_duration_sec": round(self._dur_total / self._dur_count, 1) if self._dur_count else 0,
                "max_duration_sec": round(self._dur_max, 1),
                "last_duration_sec": round(self._last_dur, 1),
                "by_end_reason": dict(sorted(self._by_reason.items())),
                "health_ok": int(self._health_ok),
                "health_fail": int(self._health_fail),
                "health_ok_rate": round(self._health_ok / hp, 4) if hp else 0,
                "engine_load": int(self._engine_load),
                "engine_unload": int(self._engine_unload),
                "last_end_reason": self._last_reason,
            }

    def dump_prom(self) -> str:
        lines = [
            "# HELP realtime_voice_attempts_total Realtime voice call attempts",
            "# TYPE realtime_voice_attempts_total counter",
            "# HELP realtime_voice_connected_total Realtime voice calls connected",
            "# TYPE realtime_voice_connected_total counter",
            "# HELP realtime_voice_active Realtime voice calls in progress",
            "# TYPE realtime_voice_active gauge",
            "# HELP realtime_voice_ended_total Realtime voice calls ended by reason",
            "# TYPE realtime_voice_ended_total counter",
            "# HELP realtime_voice_health_probe_total Voice host health probes by result",
            "# TYPE realtime_voice_health_probe_total counter",
            "# HELP realtime_voice_engine_actions_total GPU engine load/unload actions",
            "# TYPE realtime_voice_engine_actions_total counter",
        ]
        with self._lock:
            lines.append(f"realtime_voice_attempts_total {self._attempts}")
            lines.append(f"realtime_voice_connected_total {self._connected}")
            lines.append(f"realtime_voice_active {self._active}")
            for reason, n in sorted(self._by_reason.items()):
                lines.append(
                    f'realtime_voice_ended_total{{reason="{_esc(reason)}"}} {int(n)}')
            lines.append(f'realtime_voice_health_probe_total{{result="ok"}} {self._health_ok}')
            lines.append(f'realtime_voice_health_probe_total{{result="fail"}} {self._health_fail}')
            lines.append(f'realtime_voice_engine_actions_total{{action="load"}} {self._engine_load}')
            lines.append(f'realtime_voice_engine_actions_total{{action="unload"}} {self._engine_unload}')
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._attempts = 0
            self._connected = 0
            self._by_reason.clear()
            self._active = 0
            self._peak_active = 0
            self._dur_total = 0.0
            self._dur_count = 0
            self._dur_max = 0.0
            self._last_dur = 0.0
            self._health_ok = 0
            self._health_fail = 0
            self._engine_load = 0
            self._engine_unload = 0
            self._last_call_ts = 0.0
            self._last_end_ts = 0.0
            self._last_reason = ""


def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


_SINGLETON: Optional[RealtimeVoiceStats] = None
_LOCK = threading.Lock()


def get_realtime_voice_stats() -> RealtimeVoiceStats:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = RealtimeVoiceStats()
    return _SINGLETON


__all__ = ["RealtimeVoiceStats", "get_realtime_voice_stats"]
