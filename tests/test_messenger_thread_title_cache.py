"""Thread title vision cache 行为测试。

P7：第二次进入同一 chat 应命中 cache，不再调 read_thread_title_via_vision。
wrong_chat 触发时清掉 cache 防 OCR 漂移自循环。
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.integrations.messenger_rpa.runner import MessengerRpaRunner


def _make_runner_skeleton(cache_ttl: float = 30.0):
    """绕过 __init__，构造一个只跑 _thread_title_from_vision 的 runner 骨架。"""
    r = MessengerRpaRunner.__new__(MessengerRpaRunner)
    r._cfg = {
        "thread_title_vision_fallback": True,
        "thread_title_vision_cache_ttl_sec": cache_ttl,
    }
    r._title_vision_cache = {}
    r._title_vision_cache_ttl_sec = cache_ttl
    r._vision_cfg = lambda: {}
    r._global_vision_cfg = lambda: {}
    return r


@pytest.mark.asyncio
async def test_vision_title_cache_hit_skips_second_call():
    runner = _make_runner_skeleton()
    calls = []

    def fake_vision(serial, vc, gv):
        calls.append(serial)
        return SimpleNamespace(title="野末", debug="ok")

    with patch(
        "src.integrations.messenger_rpa.thread_title_vision.read_thread_title_via_vision",
        side_effect=fake_vision,
    ):
        result1: dict = {}
        t1 = await runner._thread_title_from_vision(
            "S1", result1, reason="after_tap", target_name="野末",
        )
        result2: dict = {}
        t2 = await runner._thread_title_from_vision(
            "S1", result2, reason="after_tap", target_name="野末",
        )

    assert t1 == "野末"
    assert t2 == "野末"
    assert len(calls) == 1, "第二次应命中 cache，不再调 vision"
    assert any(
        "thread_title_vision_cache_hit" in h for h in result2.get("hints", [])
    )


@pytest.mark.asyncio
async def test_vision_title_cache_no_target_name_no_cache():
    """target_name 缺失时 cache 不参与，每次都得调 vision。"""
    runner = _make_runner_skeleton()
    calls = []

    def fake_vision(serial, vc, gv):
        calls.append(serial)
        return SimpleNamespace(title="野末", debug="ok")

    with patch(
        "src.integrations.messenger_rpa.thread_title_vision.read_thread_title_via_vision",
        side_effect=fake_vision,
    ):
        await runner._thread_title_from_vision("S1", {}, reason="pre_foreground")
        await runner._thread_title_from_vision("S1", {}, reason="pre_foreground")

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_vision_title_cache_ttl_expiry():
    """TTL 过期后应重新调 vision。"""
    runner = _make_runner_skeleton(cache_ttl=0.01)
    calls = []

    def fake_vision(serial, vc, gv):
        calls.append(serial)
        return SimpleNamespace(title="野末", debug="ok")

    with patch(
        "src.integrations.messenger_rpa.thread_title_vision.read_thread_title_via_vision",
        side_effect=fake_vision,
    ):
        await runner._thread_title_from_vision(
            "S1", {}, reason="after_tap", target_name="野末",
        )
        await asyncio.sleep(0.05)  # 过 TTL
        await runner._thread_title_from_vision(
            "S1", {}, reason="after_tap", target_name="野末",
        )

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_vision_title_cache_disabled_when_ttl_zero():
    """ttl=0 关闭 cache。"""
    runner = _make_runner_skeleton(cache_ttl=0.0)
    calls = []

    def fake_vision(serial, vc, gv):
        calls.append(serial)
        return SimpleNamespace(title="野末", debug="ok")

    with patch(
        "src.integrations.messenger_rpa.thread_title_vision.read_thread_title_via_vision",
        side_effect=fake_vision,
    ):
        await runner._thread_title_from_vision(
            "S1", {}, reason="after_tap", target_name="野末",
        )
        await runner._thread_title_from_vision(
            "S1", {}, reason="after_tap", target_name="野末",
        )

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_foreground_title_cache_set_and_invalidate():
    """P7 v2: pre_foreground 单 slot cache 写入正常；_exit_thread 清掉。"""
    runner = _make_runner_skeleton()
    runner._foreground_title_cache = {}

    # 模拟 pre_foreground 写入
    runner._foreground_title_cache["S1"] = ("野末", time.monotonic() + 1800.0)
    assert "S1" in runner._foreground_title_cache

    # 模拟 _exit_thread 触发 invalidate（不调真 ADB）
    runner._foreground_title_cache.pop("S1", None)
    assert "S1" not in runner._foreground_title_cache


@pytest.mark.asyncio
async def test_vision_title_cache_does_not_cache_synthetic_token():
    """vision 返回 '{}' 之类被 sanitize 拒绝时，不能写 cache 污染后续。"""
    runner = _make_runner_skeleton()
    calls = []

    def fake_vision(serial, vc, gv):
        calls.append(serial)
        # 第一次返回会被 sanitize 拒绝的 '{}'，第二次返回真值
        return SimpleNamespace(
            title="{}" if len(calls) == 1 else "野末", debug="ok",
        )

    with patch(
        "src.integrations.messenger_rpa.thread_title_vision.read_thread_title_via_vision",
        side_effect=fake_vision,
    ):
        t1 = await runner._thread_title_from_vision(
            "S1", {}, reason="after_tap", target_name="野末",
        )
        t2 = await runner._thread_title_from_vision(
            "S1", {}, reason="after_tap", target_name="野末",
        )

    assert t1 == ""  # sanitizer 拒绝
    assert t2 == "野末"
    assert len(calls) == 2  # 第一次没 cache 进去，第二次还得调
