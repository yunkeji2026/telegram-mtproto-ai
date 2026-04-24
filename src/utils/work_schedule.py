"""
工作时间段判断（IANA 时区 + 按周几的区间列表 + 跨午夜 + 按日例外）。

- work_hours: mon…sun，每项为 [["HH:MM","HH:MM"], ...]；若 start > end 表示跨午夜
  （前一日晚间至次日早间，见下方语义）。
- work_exceptions: {"YYYY-MM-DD": [] | [["HH:MM","HH:MM"], ...]}
  - 空数组：该日全天不营业（覆盖周模板）。
  - 非空：仅使用该日列表（可含跨午夜，表示「该日晚间 + 次日早间」）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

_logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

# Python weekday: Monday=0 … Sunday=6
_WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _parse_hhmm(s: str) -> int:
    """将 'HH:MM' 转为自 0 点起的分钟数。"""
    p = (s or "").strip().split(":")
    if len(p) != 2:
        raise ValueError(f"invalid time: {s!r}")
    h, m = int(p[0]), int(p[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"invalid time: {s!r}")
    return h * 60 + m


def _normalize_ranges_unified(day_val: Any) -> List[Tuple[int, int, bool]]:
    """
    返回 [(start_min, end_min, overnight), ...]。
    overnight=True 表示 start > end（跨午夜：start 当日晚间至次日 end 之前）。
    """
    out: List[Tuple[int, int, bool]] = []
    if not day_val or not isinstance(day_val, list):
        return out
    for pair in day_val:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        a, b = str(pair[0]).strip(), str(pair[1]).strip()
        try:
            sa, sb = _parse_hhmm(a), _parse_hhmm(b)
        except ValueError:
            _logger.warning("跳过无效工作时间段: %s", pair)
            continue
        if sa <= sb:
            out.append((sa, sb, False))
        else:
            out.append((sa, sb, True))
    return out


def _get_day_list(wh: Dict[str, Any], key: str) -> Any:
    v = wh.get(key)
    if v is not None:
        return v
    for alt in (key.upper(), key.capitalize()):
        v = wh.get(alt)
        if v is not None:
            return v
    return None


def _check_today_minutes(now_min: int, ranges: List[Tuple[int, int, bool]]) -> bool:
    """当日片段：普通区间 + 跨午夜区间的「晚间」部分（now >= start）。"""
    for sa, sb, overnight in ranges:
        if overnight:
            if now_min >= sa:
                return True
        else:
            if sa <= now_min <= sb:
                return True
    return False


def _check_overnight_morning_tail(now_min: int, ranges: List[Tuple[int, int, bool]]) -> bool:
    """前一日跨午夜区间的「次日早间」部分（now <= end）。"""
    for sa, sb, overnight in ranges:
        if overnight and now_min <= sb:
            return True
    return False


def is_within_work_hours(
    utc_now: datetime,
    timezone_name: str,
    work_hours: Dict[str, Any],
    work_exceptions: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    判断 utc_now 是否落在排班内（周模板 + 可选按日例外）。

    work_exceptions 键为本地日历 YYYY-MM-DD：
    - 缺失：该日走周模板（及前一日跨午夜早间）。
    - [] 或 null：该日全天关闭（不与前一日跨午夜组合）。
    - 非空列表：该日仅使用列表内区间（覆盖周模板）；可含跨午夜。
    """
    tz_name = (timezone_name or "UTC").strip() or "UTC"
    if ZoneInfo is None:
        _logger.warning("zoneinfo 不可用，工作时间段视为未命中")
        return False
    try:
        tz = ZoneInfo(tz_name)
    except Exception as e:
        _logger.warning("无效时区 %s: %s", tz_name, e)
        return False

    if utc_now.tzinfo is None:
        utc_now = utc_now.replace(tzinfo=timezone.utc)
    local = utc_now.astimezone(tz)
    today_str = local.strftime("%Y-%m-%d")
    yesterday_str = (local - timedelta(days=1)).strftime("%Y-%m-%d")
    now_min = local.hour * 60 + local.minute
    wd = local.weekday()
    key_today = _WEEKDAY_KEYS[wd]
    key_prev = _WEEKDAY_KEYS[(wd - 1) % 7]

    we = work_exceptions if isinstance(work_exceptions, dict) else {}

    # ── 按日例外：当日 ─────────────────────────────────────
    if today_str in we:
        ex = we[today_str]
        if ex is None or (isinstance(ex, list) and len(ex) == 0):
            return False
        if isinstance(ex, list):
            ranges = _normalize_ranges_unified(ex)
            if not ranges:
                return False
            return _check_today_minutes(now_min, ranges)
        return False

    # ── 按日例外：前一日跨午夜 → 当日早间 ─────────────────
    if yesterday_str in we:
        ex = we[yesterday_str]
        if isinstance(ex, list) and ex:
            ranges = _normalize_ranges_unified(ex)
            if _check_overnight_morning_tail(now_min, ranges):
                return True

    # ── 周模板 ───────────────────────────────────────────
    if not work_hours or not isinstance(work_hours, dict):
        return False

    day_today = _get_day_list(work_hours, key_today)
    ranges_today = _normalize_ranges_unified(day_today)
    if ranges_today and _check_today_minutes(now_min, ranges_today):
        return True

    # 前一日若在 work_exceptions 中有定义（含全天休息），则不再用周模板承接其跨午夜早间
    if yesterday_str not in we:
        day_prev = _get_day_list(work_hours, key_prev)
        ranges_prev = _normalize_ranges_unified(day_prev)
        if ranges_prev and _check_overnight_morning_tail(now_min, ranges_prev):
            return True

    return False


def _align_first_coarse_minute(after_minute: int, coarse: int) -> int:
    """
    粗步进网格上第一个严格大于 ``after_minute`` 的采样点（分钟偏移）。

    细扫已覆盖 ``1..after_minute`` 时，下一采样应为 ``after_minute + 1`` 对齐到
    ``coarse`` 的倍数，避免旧实现 ``after_minute + coarse`` 在
    ``(after_minute, after_minute + coarse)`` 之间漏检。
    """
    u = max(1, int(coarse))
    n = int(after_minute) + 1
    return ((n + u - 1) // u) * u


def estimate_minutes_until_next_open(
    utc_now: datetime,
    timezone_name: str,
    work_hours: Dict[str, Any],
    work_exceptions: Optional[Dict[str, Any]] = None,
    *,
    step_minutes: int = 15,
    max_horizon_hours: int = 168,
    fine_horizon_hours: int = 24,
) -> Optional[int]:
    """
    粗估距离「下一次进入排班」的分钟数；当前已在排班内返回 0。

    两阶段：前 ``fine_horizon_hours`` 内按 **1 分钟**步进（近端更准）；
    之后按 ``step_minutes`` 粗步进至 ``max_horizon_hours``，并在命中粗网格后
    **向前回扫**至多 ``coarse-1`` 分钟，得到与 ``is_within_work_hours`` 一致的首个开窗分钟。

    ``fine_horizon_hours=0`` 时跳过细扫，仅用粗步进 + 回扫（兼容旧行为但修正漏检）。
    """
    if is_within_work_hours(utc_now, timezone_name, work_hours, work_exceptions):
        return 0
    coarse = max(1, int(step_minutes))
    max_m = max(1, max_horizon_hours * 60)

    if fine_horizon_hours and fine_horizon_hours > 0:
        fine_max = min(fine_horizon_hours * 60, max_m)
        for mm in range(1, fine_max + 1):
            t = utc_now + timedelta(minutes=mm)
            if is_within_work_hours(t, timezone_name, work_hours, work_exceptions):
                return mm
        m = _align_first_coarse_minute(fine_max, coarse)
    else:
        m = coarse

    while m <= max_m:
        t = utc_now + timedelta(minutes=m)
        if is_within_work_hours(t, timezone_name, work_hours, work_exceptions):
            lo = max(1, m - coarse + 1)
            for mm in range(lo, m + 1):
                t2 = utc_now + timedelta(minutes=mm)
                if is_within_work_hours(t2, timezone_name, work_hours, work_exceptions):
                    return mm
            return m
        m += coarse
    return None


def estimate_minutes_until_next_close(
    utc_now: datetime,
    timezone_name: str,
    work_hours: Dict[str, Any],
    work_exceptions: Optional[Dict[str, Any]] = None,
    *,
    step_minutes: int = 15,
    max_horizon_hours: int = 168,
    fine_horizon_hours: int = 24,
) -> Optional[int]:
    """
    粗估距离「下一次离开排班」的分钟数；当前不在排班内返回 None。
    两阶段与 ``estimate_minutes_until_next_open`` 相同（含粗步进对齐与回扫）。
    """
    if not is_within_work_hours(utc_now, timezone_name, work_hours, work_exceptions):
        return None
    coarse = max(1, int(step_minutes))
    max_m = max(1, max_horizon_hours * 60)

    if fine_horizon_hours and fine_horizon_hours > 0:
        fine_max = min(fine_horizon_hours * 60, max_m)
        for mm in range(1, fine_max + 1):
            t = utc_now + timedelta(minutes=mm)
            if not is_within_work_hours(t, timezone_name, work_hours, work_exceptions):
                return mm
        m = _align_first_coarse_minute(fine_max, coarse)
    else:
        m = coarse

    while m <= max_m:
        t = utc_now + timedelta(minutes=m)
        if not is_within_work_hours(t, timezone_name, work_hours, work_exceptions):
            lo = max(1, m - coarse + 1)
            for mm in range(lo, m + 1):
                t2 = utc_now + timedelta(minutes=mm)
                if not is_within_work_hours(t2, timezone_name, work_hours, work_exceptions):
                    return mm
            return m
        m += coarse
    return None
