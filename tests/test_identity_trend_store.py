"""F1：会话身份健康按日趋势落库（identity_trend_store）+ record 旁路写入。

覆盖：store add/daily/prune/全零跳过/默认关；以及 ``_record_ingest_identity`` /
``_record_avatar`` 旁路把 raw / 头像 hit·empty·total 正确映射进日聚合。
"""
from __future__ import annotations

import time

from src.web.identity_trend_store import (
    IdentityTrendStore,
    configure_identity_trend_store,
    get_identity_trend_store,
    record_identity_trend,
    reset_identity_trend_store,
)


def test_daily_zero_fills_and_rates():
    store = IdentityTrendStore(":memory:")
    now = time.time()
    # 入站 10 条：2 named + 3 backfilled + 5 raw → raw% 50%
    store.add(ing_named=2, ing_backfilled=3, ing_raw=5, now=now)
    # 头像 8 次：6 拿到图 + 2 空（总 8）→ empty% 25%, hit% 75%
    store.add(av_hit=6, av_empty=2, av_total=8, now=now)
    days = store.daily(days=7, now=now)
    assert len(days) == 7
    assert all(d["ing_total"] == 0 and d["av_total"] == 0 for d in days[:-1])  # 前 6 天补零
    today = days[-1]
    assert today["ing_total"] == 10 and today["ing_raw"] == 5
    assert today["raw_rate"] == 0.5
    assert today["av_total"] == 8 and today["av_empty"] == 2 and today["av_hit"] == 6
    assert today["empty_rate"] == 0.25
    assert today["hit_rate"] == 0.75


def test_rates_zero_when_no_denominator():
    store = IdentityTrendStore(":memory:")
    now = time.time()
    store.add(av_total=0, ing_named=0, now=now)  # 全零 → 不写
    today = store.daily(days=1, now=now)[-1]
    assert today["raw_rate"] == 0.0 and today["empty_rate"] == 0.0 and today["hit_rate"] == 0.0


def test_add_accumulates_same_day():
    store = IdentityTrendStore(":memory:")
    now = time.time()
    store.add(ing_raw=1, now=now)
    store.add(ing_raw=1, ing_named=1, now=now)
    store.add(av_hit=1, av_total=1, now=now)
    today = store.daily(days=1, now=now)[-1]
    assert today["ing_raw"] == 2 and today["ing_named"] == 1
    assert today["av_hit"] == 1 and today["av_total"] == 1


def test_add_noop_when_all_zero():
    store = IdentityTrendStore(":memory:")
    now = time.time()
    store.add(now=now)
    today = store.daily(days=1, now=now)[-1]
    assert today["ing_total"] == 0 and today["av_total"] == 0


def test_prune_drops_old_days():
    store = IdentityTrendStore(":memory:")
    now = time.time()
    store.add(ing_raw=1, now=now - 200 * 86400)
    store.add(ing_raw=1, now=now)
    assert store.prune(retention_days=90, now=now) == 1


def test_record_is_noop_until_configured():
    reset_identity_trend_store()
    record_identity_trend(ing_raw=5, av_total=5)     # 未配置 → 恒 no-op
    assert get_identity_trend_store() is None
    store = configure_identity_trend_store(enabled=True, db_path=":memory:")
    assert store is not None
    record_identity_trend(ing_raw=3, av_hit=1, av_total=1)
    today = store.daily(days=1)[-1]
    assert today["ing_raw"] == 3 and today["av_hit"] == 1 and today["av_total"] == 1
    reset_identity_trend_store()


def test_configure_disabled_keeps_noop():
    reset_identity_trend_store()
    assert configure_identity_trend_store(enabled=False) is None
    record_identity_trend(ing_raw=1)
    assert get_identity_trend_store() is None


# ─────────────── record 旁路：route helper → 趋势库映射 ───────────────

def test_record_ingest_identity_hooks_trend():
    from src.web.routes.unified_inbox_account_routes import _record_ingest_identity
    reset_identity_trend_store()
    store = configure_identity_trend_store(enabled=True, db_path=":memory:")
    _record_ingest_identity("whatsapp", "named")
    _record_ingest_identity("whatsapp", "raw")
    _record_ingest_identity("messenger", "backfilled")
    today = store.daily(days=1)[-1]
    assert today["ing_named"] == 1 and today["ing_backfilled"] == 1 and today["ing_raw"] == 1
    assert today["ing_total"] == 3 and today["raw_rate"] == round(1 / 3, 4)
    reset_identity_trend_store()


def test_record_avatar_hooks_trend_hit_empty_total():
    from src.web.routes.unified_inbox_account_routes import _record_avatar
    reset_identity_trend_store()
    store = configure_identity_trend_store(enabled=True, db_path=":memory:")
    _record_avatar("telegram", "cache_hit")   # hit
    _record_avatar("whatsapp", "fetched")     # hit
    _record_avatar("messenger", "empty")      # empty
    _record_avatar("whatsapp", "error")       # 仅计 total（非 hit 非 empty）
    _record_avatar("telegram", "neg_hit")     # 仅计 total
    today = store.daily(days=1)[-1]
    assert today["av_hit"] == 2 and today["av_empty"] == 1 and today["av_total"] == 5
    assert today["empty_rate"] == round(1 / 5, 4) and today["hit_rate"] == round(2 / 5, 4)
    reset_identity_trend_store()


def test_record_avatar_noop_when_trend_disabled():
    from src.web.routes.unified_inbox_account_routes import _record_avatar
    reset_identity_trend_store()
    _record_avatar("telegram", "fetched")     # 未配置趋势库 → 不落库、不抛
    assert get_identity_trend_store() is None
