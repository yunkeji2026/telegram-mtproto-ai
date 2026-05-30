"""设备运行统计聚合器 — 滑动窗口 5min bucket，保留 24h 数据。

每次 run_once 完成后调用 record()，内部按 5min 粒度聚合到 bucket。
前端通过 /api/rpa-overview/device-stats 拉取时间序列数据渲染图表。

内存占用估算：
  - 每个 bucket: ~6 个 int/float = 48 bytes
  - 24h = 288 buckets / platform
  - 每设备 3 平台 ≈ 42KB（可忽略不计）
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

BUCKET_SEC = 300  # 5 分钟一个 bucket
MAX_BUCKETS = 288  # 保留 24h


@dataclass
class StatBucket:
    """单个 5 分钟统计桶。"""
    ts: float = 0.0          # bucket 起始时间戳
    runs: int = 0            # run_once 次数
    ok_count: int = 0        # 成功次数
    fail_count: int = 0      # 失败次数
    replies: int = 0         # 回复次数
    total_ms: float = 0.0    # 累计耗时 ms
    circuit_open_sec: float = 0.0  # 本 bucket 内熔断总秒数


@dataclass
class PlatformStats:
    """单平台时间序列统计。"""
    platform_type: str = ""
    account_id: str = ""
    buckets: List[StatBucket] = field(default_factory=list)

    def _current_bucket(self, now: float) -> StatBucket:
        """获取或创建当前时间窗口的 bucket。"""
        bucket_ts = (int(now) // BUCKET_SEC) * BUCKET_SEC
        if self.buckets and self.buckets[-1].ts == bucket_ts:
            return self.buckets[-1]
        # 新建 bucket
        b = StatBucket(ts=bucket_ts)
        self.buckets.append(b)
        # 裁剪过期 buckets
        cutoff = now - (MAX_BUCKETS * BUCKET_SEC)
        while self.buckets and self.buckets[0].ts < cutoff:
            self.buckets.pop(0)
        return b

    def record(self, ok: bool, is_reply: bool, elapsed_ms: float, now: Optional[float] = None) -> None:
        """记录一次 run_once 结果。"""
        now = now or time.time()
        b = self._current_bucket(now)
        b.runs += 1
        if ok:
            b.ok_count += 1
        else:
            b.fail_count += 1
        if is_reply:
            b.replies += 1
        b.total_ms += elapsed_ms

    def record_circuit_open(self, duration_sec: float, now: Optional[float] = None) -> None:
        """记录熔断时长到当前 bucket。"""
        now = now or time.time()
        b = self._current_bucket(now)
        b.circuit_open_sec += duration_sec

    def summary(self, hours: float = 24.0) -> Dict[str, Any]:
        """生成 N 小时内的汇总统计。"""
        now = time.time()
        cutoff = now - (hours * 3600)
        total_runs = 0
        total_ok = 0
        total_fail = 0
        total_replies = 0
        total_ms = 0.0
        total_circuit_sec = 0.0

        for b in self.buckets:
            if b.ts < cutoff:
                continue
            total_runs += b.runs
            total_ok += b.ok_count
            total_fail += b.fail_count
            total_replies += b.replies
            total_ms += b.total_ms
            total_circuit_sec += b.circuit_open_sec

        success_rate = (total_ok / total_runs * 100) if total_runs else 0.0
        reply_rate = (total_replies / total_runs * 100) if total_runs else 0.0
        avg_ms = (total_ms / total_runs) if total_runs else 0.0

        return {
            "platform": self.platform_type,
            "account_id": self.account_id,
            "hours": hours,
            "total_runs": total_runs,
            "total_ok": total_ok,
            "total_fail": total_fail,
            "total_replies": total_replies,
            "success_rate_pct": round(success_rate, 1),
            "reply_rate_pct": round(reply_rate, 1),
            "avg_elapsed_ms": round(avg_ms, 1),
            "circuit_open_sec": round(total_circuit_sec, 1),
        }

    def timeseries(self, hours: float = 6.0) -> List[Dict[str, Any]]:
        """返回 N 小时内的 bucket 时间序列（供图表）。"""
        now = time.time()
        cutoff = now - (hours * 3600)
        result = []
        for b in self.buckets:
            if b.ts < cutoff:
                continue
            result.append({
                "ts": b.ts,
                "runs": b.runs,
                "ok": b.ok_count,
                "fail": b.fail_count,
                "replies": b.replies,
                "avg_ms": round(b.total_ms / b.runs, 1) if b.runs else 0,
            })
        return result


class DeviceStatsRegistry:
    """全局设备统计注册表 — 按 serial + platform_type 索引。"""

    def __init__(self) -> None:
        self._data: Dict[str, Dict[str, PlatformStats]] = defaultdict(dict)

    def get_or_create(self, serial: str, platform_type: str, account_id: str = "") -> PlatformStats:
        """获取或创建指定设备+平台的统计实例。"""
        pmap = self._data[serial]
        if platform_type not in pmap:
            ps = PlatformStats(platform_type=platform_type, account_id=account_id)
            pmap[platform_type] = ps
        return pmap[platform_type]

    def record(self, serial: str, platform_type: str, account_id: str,
               ok: bool, is_reply: bool, elapsed_ms: float) -> None:
        """便捷方法：记录一次运行。"""
        ps = self.get_or_create(serial, platform_type, account_id)
        ps.record(ok, is_reply, elapsed_ms)

    def device_summary(self, serial: str, hours: float = 24.0) -> Dict[str, Any]:
        """单设备全平台汇总。"""
        pmap = self._data.get(serial, {})
        platforms = []
        total_runs = 0
        total_replies = 0
        for ps in pmap.values():
            s = ps.summary(hours)
            platforms.append(s)
            total_runs += s["total_runs"]
            total_replies += s["total_replies"]

        return {
            "serial": serial,
            "hours": hours,
            "total_runs": total_runs,
            "total_replies": total_replies,
            "success_rate_pct": round(
                sum(p["total_ok"] for p in platforms) / total_runs * 100, 1
            ) if total_runs else 0.0,
            "platforms": platforms,
        }

    def all_summaries(self, hours: float = 24.0) -> List[Dict[str, Any]]:
        """所有设备汇总列表。"""
        return [self.device_summary(serial, hours) for serial in self._data]

    def device_timeseries(self, serial: str, hours: float = 6.0) -> Dict[str, Any]:
        """单设备全平台时间序列。"""
        pmap = self._data.get(serial, {})
        return {
            "serial": serial,
            "hours": hours,
            "platforms": {
                pt: ps.timeseries(hours) for pt, ps in pmap.items()
            },
        }


# 全局单例
_registry: Optional[DeviceStatsRegistry] = None


def get_device_stats() -> DeviceStatsRegistry:
    """获取全局统计注册表单例。"""
    global _registry
    if _registry is None:
        _registry = DeviceStatsRegistry()
    return _registry
