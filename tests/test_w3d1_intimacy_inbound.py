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
