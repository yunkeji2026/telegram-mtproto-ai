"""W2-D4.4：ReactivationLoop 单元测试。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.contacts.reactivation_loop import ReactivationLoop
from src.skills.reactivation_scheduler import ReactivationCandidate


# ── 辅助：构造 mock store / scheduler / candidate ─────────────────

@dataclass
class _StubChannelIdentity:
    channel: str = "messenger"
    account_id: str = "bg_phone_2"
    external_id: str = "Alice"
    display_name: str = "Alice"


@dataclass
class _StubContact:
    contact_id: str = "c1"
    primary_name: str = "Alice"
    language_hint: str = "ja"
    timezone_hint: str = "Asia/Tokyo"


@dataclass
class _StubJourney:
    journey_id: str = "j1"
    context_snapshot_json: str = ""


@dataclass
class _StubStore:
    contact: _StubContact = field(default_factory=_StubContact)
    journey: _StubJourney = field(default_factory=_StubJourney)
    identities: List[_StubChannelIdentity] = field(
        default_factory=lambda: [_StubChannelIdentity()],
    )

    def get_contact(self, cid):
        return self.contact

    def list_channel_identities_of(self, cid):
        return self.identities

    def get_journey_by_contact(self, cid):
        return self.journey


def _make_candidate(cid="c1", silent_days=5.0, intimacy=70.0,
                    stage="LINE_ENGAGED"):
    return ReactivationCandidate(
        journey_id="j1", contact_id=cid, funnel_stage=stage,
        intimacy_score=intimacy, silent_days=silent_days,
        last_reactivation_ts=0,
    )


@pytest.fixture
def base_setup():
    """统一构造 happy 路径所需 mock。"""
    scheduler = MagicMock()
    scheduler.list_candidates = MagicMock(return_value=[_make_candidate()])
    scheduler.mark_sent = MagicMock()

    store = _StubStore()
    ai_client = MagicMock()
    ai_client.chat = AsyncMock(return_value="嘿，上次你说面试，怎么样啦？")

    send_callback = AsyncMock(return_value=42)  # 返 row_id=42 = 成功
    episodic_provider = MagicMock(return_value="- 上次提过：要面试新公司")

    return dict(
        scheduler=scheduler, store=store, ai_client=ai_client,
        send_callback=send_callback, episodic_provider=episodic_provider,
    )


def _make_loop(base_setup, **overrides):
    kw = dict(
        ai_name="Lily", max_per_tick=3, interval_sec=600,
        first_run_grace_minutes=0,  # 测试默认关掉宽限期，免得限速干扰
    )
    kw.update(overrides)
    return ReactivationLoop(**base_setup, **kw)


# ── 测试 ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_happy_path_one_candidate(base_setup):
    loop = _make_loop(base_setup)
    n = await loop.run_once()
    assert n == 1
    assert base_setup["send_callback"].await_count == 1
    base_setup["scheduler"].mark_sent.assert_called_once()


@pytest.mark.asyncio
async def test_skips_when_no_messenger_identity(base_setup):
    base_setup["store"].identities = [
        _StubChannelIdentity(channel="line"),  # 只有 line，无 messenger
    ]
    loop = _make_loop(base_setup)
    n = await loop.run_once()
    assert n == 0
    base_setup["send_callback"].assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_when_llm_empty(base_setup):
    base_setup["ai_client"].chat = AsyncMock(return_value="")
    loop = _make_loop(base_setup)
    assert await loop.run_once() == 0
    base_setup["send_callback"].assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_when_llm_too_short(base_setup):
    base_setup["ai_client"].chat = AsyncMock(return_value="嗯")
    loop = _make_loop(base_setup)
    assert await loop.run_once() == 0


@pytest.mark.asyncio
async def test_skips_when_llm_out_of_persona(base_setup):
    base_setup["ai_client"].chat = AsyncMock(
        return_value="作为AI助手，我建议你尽快回复...",
    )
    loop = _make_loop(base_setup)
    assert await loop.run_once() == 0
    base_setup["send_callback"].assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_when_no_episodic_with_flag(base_setup):
    base_setup["episodic_provider"] = MagicMock(return_value="")
    loop = _make_loop(base_setup, skip_if_no_episodic=True)
    assert await loop.run_once() == 0
    base_setup["ai_client"].chat.assert_not_awaited()  # 连 LLM 都不调


@pytest.mark.asyncio
async def test_keeps_going_when_no_episodic_with_flag_off(base_setup):
    base_setup["episodic_provider"] = MagicMock(return_value="")
    loop = _make_loop(base_setup, skip_if_no_episodic=False)
    n = await loop.run_once()
    assert n == 1
    base_setup["ai_client"].chat.assert_awaited()


@pytest.mark.asyncio
async def test_dry_run_does_not_enqueue(base_setup):
    loop = _make_loop(base_setup, dry_run=True)
    n = await loop.run_once()
    assert n == 1  # 仍计入"已调度"
    base_setup["send_callback"].assert_not_awaited()  # 但没真 enqueue
    base_setup["scheduler"].mark_sent.assert_not_called()  # 也没 mark_sent


@pytest.mark.asyncio
async def test_send_callback_returns_zero_means_failed(base_setup):
    base_setup["send_callback"] = AsyncMock(return_value=0)
    loop = _make_loop(base_setup)
    n = await loop.run_once()
    assert n == 0
    base_setup["scheduler"].mark_sent.assert_not_called()


@pytest.mark.asyncio
async def test_max_per_tick_limits_candidates(base_setup):
    base_setup["scheduler"].list_candidates = MagicMock(
        return_value=[_make_candidate(cid=f"c{i}") for i in range(5)],
    )
    loop = _make_loop(base_setup, max_per_tick=2)
    n = await loop.run_once()
    assert n == 2  # 5 个候选只调度 2


@pytest.mark.asyncio
async def test_first_run_grace_caps_to_one(base_setup):
    """启动后宽限期内即使 max_per_tick=5 也只能调度 1 条"""
    base_setup["scheduler"].list_candidates = MagicMock(
        return_value=[_make_candidate(cid=f"c{i}") for i in range(5)],
    )
    loop = _make_loop(
        base_setup, max_per_tick=5,
        first_run_grace_minutes=10,  # 宽限 10 分钟
        first_run_max_per_tick=1,
    )
    loop._started_ts = time.time()  # 模拟刚启动
    n = await loop.run_once()
    assert n == 1


@pytest.mark.asyncio
async def test_grace_expires_after_window(base_setup):
    """宽限期过后回到正常 max_per_tick"""
    base_setup["scheduler"].list_candidates = MagicMock(
        return_value=[_make_candidate(cid=f"c{i}") for i in range(5)],
    )
    loop = _make_loop(
        base_setup, max_per_tick=3,
        first_run_grace_minutes=0.001,  # 6 秒前已过期
        first_run_max_per_tick=1,
    )
    loop._started_ts = time.time() - 100  # 假装启动 100 秒前
    n = await loop.run_once()
    assert n == 3  # 宽限过了，恢复 max_per_tick


@pytest.mark.asyncio
async def test_no_candidates_returns_zero_quickly(base_setup):
    base_setup["scheduler"].list_candidates = MagicMock(return_value=[])
    loop = _make_loop(base_setup)
    n = await loop.run_once()
    assert n == 0
    base_setup["ai_client"].chat.assert_not_awaited()
