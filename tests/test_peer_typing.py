"""W2-D3.4 + D4.8/4.9：peer_typing detector 单测。"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.integrations.messenger_rpa.peer_typing import (
    NullPeerTypingDetector,
    PeerTypingResult,
    VisionPeerTypingDetector,
    build_peer_typing_detector,
)


# ── 工厂行为 ────────────────────────────────────────

def test_factory_disabled_returns_null():
    d = build_peer_typing_detector({"enabled": False})
    assert isinstance(d, NullPeerTypingDetector)


def test_factory_no_backend_returns_null():
    d = build_peer_typing_detector({"enabled": True, "backend": "null"})
    assert isinstance(d, NullPeerTypingDetector)


def test_factory_vision_no_client_returns_null():
    """开了但没注入 vision_client → 回退 Null（不崩）"""
    d = build_peer_typing_detector(
        {"enabled": True, "backend": "vision"}, vision_client=None,
    )
    assert isinstance(d, NullPeerTypingDetector)


def test_factory_vision_with_client():
    vc = MagicMock()
    d = build_peer_typing_detector(
        {"enabled": True, "backend": "vision"}, vision_client=vc,
    )
    assert isinstance(d, VisionPeerTypingDetector)


# ── Null detector ───────────────────────────────────

@pytest.mark.asyncio
async def test_null_always_false():
    d = NullPeerTypingDetector()
    r = await d.detect("/some/path.png", chat_key="acc:Alice")
    assert r.is_typing is False
    assert r.confidence == 0.0


# ── Vision detector ─────────────────────────────────

@pytest.fixture
def fake_screenshot(tmp_path: Path) -> str:
    """生成一个 100x200 红色 PNG 用于 crop 测试。"""
    pytest.importorskip("PIL")
    from PIL import Image
    p = tmp_path / "fake_shot.png"
    Image.new("RGB", (100, 200), color="red").save(p)
    return str(p)


@pytest.mark.asyncio
async def test_vision_detects_yes(fake_screenshot):
    vc = MagicMock()
    vc.describe_image = AsyncMock(return_value="Yes")
    d = VisionPeerTypingDetector(vc, cache_sec=0)
    r = await d.detect(fake_screenshot, chat_key="acc:Alice")
    assert r.is_typing is True
    assert r.suggested_wait_sec > 0
    vc.describe_image.assert_awaited_once()


@pytest.mark.asyncio
async def test_vision_detects_no(fake_screenshot):
    vc = MagicMock()
    vc.describe_image = AsyncMock(return_value="No")
    d = VisionPeerTypingDetector(vc, cache_sec=0)
    r = await d.detect(fake_screenshot)
    assert r.is_typing is False
    assert r.suggested_wait_sec == 0


@pytest.mark.asyncio
async def test_vision_unclear_response_treated_as_no(fake_screenshot):
    vc = MagicMock()
    vc.describe_image = AsyncMock(return_value="I cannot tell from this image.")
    d = VisionPeerTypingDetector(vc, cache_sec=0)
    r = await d.detect(fake_screenshot)
    assert r.is_typing is False  # 拒答 → 保守 no


@pytest.mark.asyncio
async def test_vision_cache_hit_avoids_second_call(fake_screenshot):
    vc = MagicMock()
    vc.describe_image = AsyncMock(return_value="yes")
    d = VisionPeerTypingDetector(vc, cache_sec=10.0)  # 长 cache
    r1 = await d.detect(fake_screenshot, chat_key="acc:Alice")
    r2 = await d.detect(fake_screenshot, chat_key="acc:Alice")
    assert r1 == r2
    # 第二次走 cache，vision 只调一次
    assert vc.describe_image.await_count == 1


@pytest.mark.asyncio
async def test_vision_cache_miss_for_different_chat(fake_screenshot):
    vc = MagicMock()
    vc.describe_image = AsyncMock(return_value="yes")
    d = VisionPeerTypingDetector(vc, cache_sec=10.0)
    await d.detect(fake_screenshot, chat_key="acc:Alice")
    await d.detect(fake_screenshot, chat_key="acc:Bob")
    assert vc.describe_image.await_count == 2  # 不同 chat，独立调


@pytest.mark.asyncio
async def test_vision_call_failure_fail_open(fake_screenshot):
    """vision 调用异常 → 当作 not_typing（fail-open，不卡主流程）"""
    vc = MagicMock()
    vc.describe_image = AsyncMock(side_effect=RuntimeError("vision down"))
    d = VisionPeerTypingDetector(vc, cache_sec=0)
    r = await d.detect(fake_screenshot)
    assert r.is_typing is False


@pytest.mark.asyncio
async def test_vision_missing_screenshot_path():
    vc = MagicMock()
    vc.describe_image = AsyncMock(return_value="yes")
    d = VisionPeerTypingDetector(vc)
    r = await d.detect("/non/existent/path.png", chat_key="acc:Alice")
    assert r.is_typing is False
    vc.describe_image.assert_not_awaited()


@pytest.mark.asyncio
async def test_vision_clear_cache(fake_screenshot):
    vc = MagicMock()
    vc.describe_image = AsyncMock(return_value="yes")
    d = VisionPeerTypingDetector(vc, cache_sec=10.0)
    await d.detect(fake_screenshot, chat_key="acc:Alice")
    d.clear_cache()
    await d.detect(fake_screenshot, chat_key="acc:Alice")
    assert vc.describe_image.await_count == 2


@pytest.mark.asyncio
async def test_vision_yes_chinese(fake_screenshot):
    """中文 "是" 也能识别"""
    vc = MagicMock()
    vc.describe_image = AsyncMock(return_value="是的")
    d = VisionPeerTypingDetector(vc, cache_sec=0)
    r = await d.detect(fake_screenshot)
    assert r.is_typing is True


@pytest.mark.asyncio
async def test_vision_crop_creates_temp_then_cleanup(fake_screenshot, tmp_path):
    """crop 后临时文件应被清理（D5.4：用 mkstemp 唯一名）"""
    import glob
    vc = MagicMock()
    vc.describe_image = AsyncMock(return_value="no")
    d = VisionPeerTypingDetector(vc, cache_sec=0)
    # 跑 detect 之前，临时目录里没有 peer_typing_crop_ 残留
    leftover_before = glob.glob(
        os.path.join(tempfile.gettempdir(), "peer_typing_crop_*"),
    )
    await d.detect(fake_screenshot)
    leftover_after = glob.glob(
        os.path.join(tempfile.gettempdir(), "peer_typing_crop_*"),
    )
    # 跑完 detect 后不该比之前多文件（说明清理了）
    assert len(leftover_after) <= len(leftover_before)


# ── W2-D5.2 timeout ─────────────────────────────────

@pytest.mark.asyncio
async def test_vision_timeout_fails_open(fake_screenshot):
    """vision 慢于 timeout 时回 not_typing 不卡住"""
    import asyncio as _aio

    async def slow_vision(*args, **kwargs):
        await _aio.sleep(10.0)
        return "yes"

    vc = MagicMock()
    vc.describe_image = AsyncMock(side_effect=slow_vision)
    d = VisionPeerTypingDetector(vc, cache_sec=0, timeout_sec=0.2)
    import time as _t
    t0 = _t.monotonic()
    r = await d.detect(fake_screenshot)
    elapsed = _t.monotonic() - t0
    assert r.is_typing is False
    # 语义：应在 0.2s 超时后快速 fail-open，而非阻塞等满 vision 调用。
    # 阈值取 5.0s（远低于 10s vision、远高于 0.2s 超时），容忍 -n auto 并行下的
    # 事件循环调度抖动，避免偶发 flaky；只要远早于 vision 完成即证明 fail-open。
    assert elapsed < 5.0, f"应快速超时 fail-open，实际 {elapsed:.2f}s"


# ── W2-D5.3 sample_rate ─────────────────────────────

@pytest.mark.asyncio
async def test_sample_rate_zero_skips_all(fake_screenshot):
    vc = MagicMock()
    vc.describe_image = AsyncMock(return_value="yes")
    d = VisionPeerTypingDetector(vc, cache_sec=0, sample_rate=0.0)
    r = await d.detect(fake_screenshot, chat_key="any")
    assert r.is_typing is False
    vc.describe_image.assert_not_awaited()


@pytest.mark.asyncio
async def test_sample_rate_one_calls_all(fake_screenshot):
    vc = MagicMock()
    vc.describe_image = AsyncMock(return_value="yes")
    d = VisionPeerTypingDetector(vc, cache_sec=0, sample_rate=1.0)
    r = await d.detect(fake_screenshot, chat_key="any")
    assert r.is_typing is True
    vc.describe_image.assert_awaited()


def test_sample_rate_sticky_per_chat():
    """同一 chat_key 多次评估，结果稳定（hash sticky）"""
    d = VisionPeerTypingDetector(MagicMock(), sample_rate=0.5)
    r1 = d._should_sample("chat_A")
    r2 = d._should_sample("chat_A")
    r3 = d._should_sample("chat_A")
    assert r1 == r2 == r3


def test_sample_rate_different_chats_distribute():
    """50% 采样下，大量 chat 大约一半被采"""
    d = VisionPeerTypingDetector(MagicMock(), sample_rate=0.5)
    sampled = sum(d._should_sample(f"chat_{i}") for i in range(200))
    # 接近 100，允许 ±30%（统计方差）
    assert 70 < sampled < 130


def test_sample_rate_no_chat_key_fallback_true():
    """空 chat_key → 默认采样（不能 0 概率永远跳过）"""
    d = VisionPeerTypingDetector(MagicMock(), sample_rate=0.5)
    assert d._should_sample("") is True


# ── W2-D5.4 race-safe temp file ─────────────────────

@pytest.mark.asyncio
async def test_concurrent_detect_no_temp_file_collision(fake_screenshot):
    """两个并发 detect 同时跑，不应该互相覆盖临时文件"""
    import asyncio as _aio
    vc = MagicMock()
    # 每次 vision 调用响应稍慢点，模拟并发 race window
    async def slow_yes(*args, **kwargs):
        await _aio.sleep(0.05)
        return "yes"
    vc.describe_image = AsyncMock(side_effect=slow_yes)
    d = VisionPeerTypingDetector(vc, cache_sec=0)
    # 开 5 个并发 detect，全部应该成功（不抛 FileNotFound 等）
    results = await _aio.gather(*[
        d.detect(fake_screenshot, chat_key=f"chat_{i}")
        for i in range(5)
    ])
    assert all(r.is_typing for r in results)
