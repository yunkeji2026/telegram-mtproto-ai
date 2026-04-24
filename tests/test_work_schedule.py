"""工作时间段判断单元测试（含跨午夜、按日例外）。"""

from datetime import datetime, timezone

import pytest

from src.utils.work_schedule import (
    estimate_minutes_until_next_close,
    estimate_minutes_until_next_open,
    is_within_work_hours,
)


def test_empty_work_hours_false():
    assert not is_within_work_hours(
        datetime.now(timezone.utc), "Asia/Shanghai", {}
    )


def test_monday_inside_window_shanghai():
    wh = {"mon": [["09:00", "18:00"]]}
    utc = datetime(2025, 3, 10, 10, 0, 0, tzinfo=timezone.utc)
    assert is_within_work_hours(utc, "Asia/Shanghai", wh)


def test_monday_outside_window_shanghai():
    wh = {"mon": [["09:00", "12:00"]]}
    utc = datetime(2025, 3, 10, 6, 0, 0, tzinfo=timezone.utc)
    assert not is_within_work_hours(utc, "Asia/Shanghai", wh)


def test_saturday_empty_day():
    wh = {"sat": [], "mon": [["09:00", "18:00"]]}
    utc = datetime(2025, 3, 15, 10, 0, 0, tzinfo=timezone.utc)
    assert not is_within_work_hours(utc, "Asia/Shanghai", wh)


def test_overnight_monday_evening():
    """周一 22:00 上海：跨午夜区间的晚间段。"""
    wh = {"mon": [["22:00", "06:00"]]}
    utc = datetime(2025, 3, 10, 14, 0, 0, tzinfo=timezone.utc)
    assert is_within_work_hours(utc, "Asia/Shanghai", wh)


def test_overnight_tuesday_morning_tail():
    """周二 01:00 上海：承接周一 22:00–次日 06:00 的早间段。"""
    wh = {"mon": [["22:00", "06:00"]]}
    utc = datetime(2025, 3, 10, 17, 0, 0, tzinfo=timezone.utc)
    assert is_within_work_hours(utc, "Asia/Shanghai", wh)


def test_exception_closed_overrides_weekday():
    wh = {"mon": [["09:00", "18:00"]]}
    we = {"2025-03-10": []}
    utc = datetime(2025, 3, 10, 2, 0, 0, tzinfo=timezone.utc)
    assert not is_within_work_hours(utc, "Asia/Shanghai", wh, we)


def test_exception_override_hours():
    wh = {"mon": [["09:00", "18:00"]]}
    we = {"2025-03-10": [["10:00", "11:00"]]}
    utc = datetime(2025, 3, 10, 2, 0, 0, tzinfo=timezone.utc)
    assert is_within_work_hours(utc, "Asia/Shanghai", wh, we)
    utc2 = datetime(2025, 3, 10, 4, 0, 0, tzinfo=timezone.utc)
    assert not is_within_work_hours(utc2, "Asia/Shanghai", wh, we)


def test_exception_yesterday_overnight_morning():
    """按日例外中的跨午夜：前一日晚间 → 次日早间。"""
    we = {"2025-03-10": [["22:00", "06:00"]]}
    wh = {"tue": [["09:00", "18:00"]]}
    utc = datetime(2025, 3, 10, 17, 0, 0, tzinfo=timezone.utc)
    assert is_within_work_hours(utc, "Asia/Shanghai", wh, we)


def test_weekday_overnight_suppressed_if_yesterday_exception():
    """前一日在 work_exceptions 中定义（含休息），不再用周模板承接跨午夜早间。"""
    wh = {"mon": [["22:00", "06:00"]], "tue": [["09:00", "18:00"]]}
    we = {"2025-03-10": []}
    utc = datetime(2025, 3, 10, 17, 0, 0, tzinfo=timezone.utc)
    assert not is_within_work_hours(utc, "Asia/Shanghai", wh, we)


def test_estimate_minutes_open_zero_when_inside():
    wh = {"mon": [["09:00", "18:00"]]}
    utc = datetime(2025, 3, 10, 10, 0, 0, tzinfo=timezone.utc)
    assert estimate_minutes_until_next_open(utc, "Asia/Shanghai", wh, {}) == 0


def test_estimate_close_when_inside():
    wh = {"mon": [["09:00", "12:00"]]}
    # 周一 10:00 上海 = 02:00 UTC
    utc = datetime(2025, 3, 10, 2, 0, 0, tzinfo=timezone.utc)
    assert estimate_minutes_until_next_close(utc, "Asia/Shanghai", wh, {}) is not None


def test_estimate_open_fine_beats_coarse_only():
    """细扫能命中开窗前 1 分钟级；仅粗步进时第一步可能已越过整点开窗。"""
    wh = {"mon": [["09:00", "12:00"]]}
    # 周一 08:46 上海 = 周一 00:46 UTC
    utc = datetime(2025, 3, 10, 0, 46, 0, tzinfo=timezone.utc)
    assert not is_within_work_hours(utc, "Asia/Shanghai", wh, {})
    fine = estimate_minutes_until_next_open(
        utc, "Asia/Shanghai", wh, {}, step_minutes=15, fine_horizon_hours=24
    )
    coarse_only = estimate_minutes_until_next_open(
        utc, "Asia/Shanghai", wh, {}, step_minutes=15, fine_horizon_hours=0
    )
    assert fine == 14
    assert coarse_only == 14


def test_estimate_open_coarse_grid_align_and_refine():
    """细扫 1h 未命中后，粗网格对齐 + 回扫，不漏掉 fine_max+1.. 的首开窗分钟。"""
    wh = {"mon": [["09:00", "12:00"]]}
    # 周一 07:59 上海 = 周日 23:59 UTC；距 09:00 开窗 = 61 分钟
    utc = datetime(2025, 3, 9, 23, 59, 0, tzinfo=timezone.utc)
    assert not is_within_work_hours(utc, "Asia/Shanghai", wh, {})
    m = estimate_minutes_until_next_open(
        utc,
        "Asia/Shanghai",
        wh,
        {},
        step_minutes=15,
        fine_horizon_hours=1,
        max_horizon_hours=24,
    )
    assert m == 61
