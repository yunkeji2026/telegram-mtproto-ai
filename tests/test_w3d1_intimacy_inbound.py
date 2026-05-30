"""W3-D1.1：intimacy_engine 接入 inbound 链路 — 验证 msg_in 自动触发 refresh。

修复 bug：之前 intimacy_engine 完全没接入 ContactGateway.on_message，所有
journey 的 intimacy_score 永远是 0，导致 reactivation_scheduler 候选数永远为 0。
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.contacts.gateway import ContactGateway
from src.contacts.handoff import HandoffTokenService
from src.contacts.merge import MergeService
from src.contacts.models import CHANNEL_MESSENGER
from src.contacts.store import ContactStore
from src.skills.intimacy_engine import IntimacyEngine


@pytest.fixture
def env(tmp_path: Path):
    store = ContactStore(db_path=tmp_path / "contacts.db")
    gw = ContactGateway(
        store,
        HandoffTokenService(store, ttl_seconds=3600),
        MergeService(store),
    )
    yield store, gw
    store.close()


def test_msg_in_without_engine_keeps_intimacy_zero(env):
    """没注入 engine 时，msg_in 不影响 intimacy（旧兼容行为）"""
    store, gw = env
    ctx = gw.on_message(
        channel=CHANNEL_MESSENGER, account_id="a", external_id="fb1",
        direction="in", text_preview="hi",
    )
    journey = store.get_journey_by_contact(ctx.contact.contact_id)
    assert journey.intimacy_score == 0.0


def test_msg_in_with_engine_refreshes_intimacy(env):
    """注入 engine 后，msg_in 自动触发 refresh → score 变化"""
    store, gw = env
    engine = IntimacyEngine(store)
    gw.set_intimacy_engine(engine)
    ctx = gw.on_message(
        channel=CHANNEL_MESSENGER, account_id="a", external_id="fb1",
        direction="in", text_preview="hi",
    )
    journey = store.get_journey_by_contact(ctx.contact.contact_id)
    # 1 条 msg_in 应该让 intimacy_score > 0（具体数字由 engine 算法决定）
    assert journey.intimacy_score > 0


def test_msg_out_does_not_trigger_refresh(env):
    """msg_out 不触发 refresh（节省算力，且 outbound 不该影响"亲密度"信号）"""
    store, gw = env
    engine = MagicMock()
    engine.refresh_journey_intimacy = MagicMock()
    gw.set_intimacy_engine(engine)
    gw.on_message(
        channel=CHANNEL_MESSENGER, account_id="a", external_id="fb1",
        direction="out", text_preview="hello",
    )
    engine.refresh_journey_intimacy.assert_not_called()


def test_engine_failure_does_not_break_message_handling(env):
    """engine 抛异常时 on_message 仍正常返回（fail-open）"""
    store, gw = env
    bad_engine = MagicMock()
    bad_engine.refresh_journey_intimacy = MagicMock(
        side_effect=RuntimeError("engine boom"),
    )
    gw.set_intimacy_engine(bad_engine)
    ctx = gw.on_message(
        channel=CHANNEL_MESSENGER, account_id="a", external_id="fb1",
        direction="in", text_preview="hi",
    )
    assert ctx is not None
    # journey 仍创建并 transit
    journey = store.get_journey_by_contact(ctx.contact.contact_id)
    assert journey is not None


def test_multiple_inbound_keeps_engine_called(env):
    """连续多条 msg_in 每条都触发 engine"""
    store, gw = env
    engine = MagicMock()
    engine.refresh_journey_intimacy = MagicMock()
    gw.set_intimacy_engine(engine)
    for i in range(5):
        gw.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb1",
            direction="in", text_preview=f"msg{i}",
        )
    assert engine.refresh_journey_intimacy.call_count == 5


# ── W3-3A.1（2026-05-17）：LINE on_line_first_text 也应触发 refresh ──
# 修复 silent gap：之前 LINE 渠道入库走 on_line_first_text 而非 on_message，
# 导致 intimacy_score 在 LINE 上永远 0；companion_relationship 融合在 LINE 完全无效。

def test_line_first_text_with_engine_refreshes_intimacy(env):
    """注入 engine 后，on_line_first_text 也触发 refresh（与 msg_in 对齐）。"""
    from src.contacts.models import CHANNEL_LINE
    store, gw = env
    engine = MagicMock()
    engine.refresh_journey_intimacy = MagicMock()
    gw.set_intimacy_engine(engine)
    gw.on_line_first_text(
        account_id="a", external_id="line_x", text="hello",
    )
    engine.refresh_journey_intimacy.assert_called_once()


def test_line_first_text_replay_also_refreshes(env):
    """重放路径同样要刷新 — runner 把所有 LINE inbound 都送 on_line_first_text，
    不刷新会导致首条之后 score 永远定格（修复 W3-3A.1 silent gap）。
    """
    store, gw = env
    engine = MagicMock()
    engine.refresh_journey_intimacy = MagicMock()
    gw.set_intimacy_engine(engine)
    # 第一次 → 走完整路径
    gw.on_line_first_text(account_id="a", external_id="line_x", text="hello")
    # 第二次（重放路径，已记录 line_first_reply）→ 早返回但仍要刷新
    gw.on_line_first_text(account_id="a", external_id="line_x", text="hello again")
    assert engine.refresh_journey_intimacy.call_count == 2


def test_line_first_text_engine_failure_does_not_break(env):
    """engine 抛异常时 LINE 首条仍能完成合并流程（fail-open）。"""
    store, gw = env
    bad = MagicMock()
    bad.refresh_journey_intimacy = MagicMock(side_effect=RuntimeError("boom"))
    gw.set_intimacy_engine(bad)
    out = gw.on_line_first_text(
        account_id="a", external_id="line_x", text="hi",
    )
    assert out is not None  # 没崩
