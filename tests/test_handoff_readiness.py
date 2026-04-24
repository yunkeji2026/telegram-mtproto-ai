"""HandoffReadinessScorer 单元测试。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.contacts.store import ContactStore
from src.contacts.handoff import HandoffTokenService
from src.contacts.merge import MergeService
from src.contacts.gateway import ContactGateway
from src.contacts.models import CHANNEL_MESSENGER
from src.skills.intimacy_engine import IntimacyEngine
from src.skills.handoff_readiness import (
    HandoffReadinessScorer,
    is_goodbye_text,
)


@pytest.fixture
def env(tmp_path):
    store = ContactStore(db_path=tmp_path / "contacts.db")
    gw = ContactGateway(store, HandoffTokenService(store, ttl_seconds=3600), MergeService(store))
    intim = IntimacyEngine(store)
    scorer = HandoffReadinessScorer(store, intim, turn_saturation=3, open_threshold=70.0)
    yield store, gw, intim, scorer
    store.close()


def _seed_chat(gw, *, in_count, out_count, fb_id="fb_1"):
    ctx = gw.on_peer_seen(channel=CHANNEL_MESSENGER, account_id="a", external_id=fb_id,
                          display_name="Alice")
    for i in range(in_count):
        gw.on_message(channel=CHANNEL_MESSENGER, account_id="a", external_id=fb_id,
                       direction="in", text_preview=f"msg_in {i}")
    for i in range(out_count):
        gw.on_message(channel=CHANNEL_MESSENGER, account_id="a", external_id=fb_id,
                       direction="out", text_preview=f"msg_out {i}")
    return ctx.journey.journey_id


class TestGoodbyeDetection:
    def test_zh_hits(self):
        assert is_goodbye_text("我去睡了 晚安")
        assert is_goodbye_text("今天先这样吧")
        assert is_goodbye_text("改天聊～")

    def test_en_hits(self):
        assert is_goodbye_text("gotta go")
        assert is_goodbye_text("Good night!")
        assert is_goodbye_text("ttyl")

    def test_misses(self):
        assert not is_goodbye_text("你在吗")
        assert not is_goodbye_text("hi how are you")
        assert not is_goodbye_text("")


class TestReadinessScoring:
    def test_cold_start_low_score(self, env):
        _, gw, _, scorer = env
        jid = _seed_chat(gw, in_count=0, out_count=0)
        d = scorer.evaluate(jid, latest_in_text="")
        assert d.score < 20
        assert d.window_open is False

    def test_no_goodbye_never_opens_window(self, env):
        """合约测试：不管 score 多高，没 goodbye 就不开窗。"""
        _, gw, _, scorer = env
        jid = _seed_chat(gw, in_count=20, out_count=20)
        d = scorer.evaluate(jid, latest_in_text="你今天干嘛了")
        assert d.window_open is False

    def test_goodbye_with_sufficient_score_opens(self, env):
        """跨天聊够多 → score 达标 + 告别 → 开窗。"""
        store, gw, intim, scorer = env
        # 建 journey 然后直接 insert 跨天事件（保证 active_days=5）
        ctx = gw.on_peer_seen(channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1")
        jid = ctx.journey.journey_id
        now = int(time.time())
        import uuid
        with store._lock:
            for d in range(5):       # 连续 5 天
                for i in range(4):   # 每天 4 in + 4 out
                    for et in ("msg_in", "msg_out"):
                        store._conn.execute(
                            "INSERT INTO journey_events (event_id, journey_id, trace_id, event_type, payload_json, ts) "
                            "VALUES (?, ?, '', ?, '{}', ?)",
                            (uuid.uuid4().hex, jid, et, now - d * 86400 - i * 60),
                        )
            store._conn.commit()
        d = scorer.evaluate(jid, latest_in_text="我去睡了 晚安")
        assert d.score >= 70
        assert d.window_open is True

    def test_goodbye_alone_insufficient(self, env):
        _, gw, _, scorer = env
        # 只有 1 条 in 1 条 out
        jid = _seed_chat(gw, in_count=1, out_count=1)
        d = scorer.evaluate(jid, latest_in_text="我去睡了 晚安")
        # 分数不到 70 —— goodbye 加 0.15 权重但 intimacy/turn 不够
        assert d.score < 70
        assert d.window_open is False

    def test_turn_saturation(self, env):
        _, gw, _, scorer = env
        # 3 条 in 已达 sat
        jid3 = _seed_chat(gw, in_count=3, out_count=3, fb_id="fb_3")
        d3 = scorer.evaluate(jid3, latest_in_text="")
        # 20 条更多
        jid20 = _seed_chat(gw, in_count=20, out_count=20, fb_id="fb_20")
        d20 = scorer.evaluate(jid20, latest_in_text="")
        # turn 贡献等（都是 sat 满），但 intimacy 的 turn_count_in 信号让 d20 更高
        assert d3.contributions["turn_count"] == d20.contributions["turn_count"]

    def test_contributions_sum_matches_score(self, env):
        _, gw, _, scorer = env
        jid = _seed_chat(gw, in_count=5, out_count=5)
        d = scorer.evaluate(jid, latest_in_text="晚安")
        sum_contribs = sum(d.contributions.values())
        # score 是 *100 round 1 位；contribs 是 0-1 round 3 位
        assert abs(round(sum_contribs * 100, 1) - d.score) < 0.2

    def test_threshold_configurable(self, env):
        store, gw, intim, _ = env
        custom = HandoffReadinessScorer(store, intim, turn_saturation=3, open_threshold=40.0)
        jid = _seed_chat(gw, in_count=3, out_count=3)
        d = custom.evaluate(jid, latest_in_text="")
        # score 可能在 40-70 之间，低阈值放宽时更容易 score>=threshold
        # 但 window 仍要 goodbye 才开——这里没 goodbye
        assert d.window_open is False
        d2 = custom.evaluate(jid, latest_in_text="晚安啦")
        if d2.score >= 40.0:
            assert d2.window_open is True
