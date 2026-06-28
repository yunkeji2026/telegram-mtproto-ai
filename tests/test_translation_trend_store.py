"""S：翻译置信度按日趋势落库（translation_trend_store）+ 路由旁路写入。"""

from __future__ import annotations

import time

import pytest

from src.ai.translation_trend_store import (
    TranslationTrendStore,
    configure_translation_trend_store,
    get_translation_trend_store,
    record_translation_trend,
    reset_translation_trend_store,
)


def test_daily_zero_fills_and_rates():
    store = TranslationTrendStore(":memory:")
    now = time.time()
    store.add(attempts=10, low_conf=4, switches=2, now=now)
    days = store.daily(days=7, now=now)
    assert len(days) == 7
    # 前 6 天补零、不断点
    assert all(d["attempts"] == 0 for d in days[:-1])
    today = days[-1]
    assert today["attempts"] == 10
    assert today["low_conf"] == 4
    assert today["switches"] == 2
    assert today["low_conf_rate"] == 0.4
    assert today["switch_rate"] == 0.2


def test_add_accumulates_same_day():
    store = TranslationTrendStore(":memory:")
    now = time.time()
    store.add(attempts=1, now=now)
    store.add(attempts=1, low_conf=1, now=now)
    store.add(switches=1, now=now)
    today = store.daily(days=1, now=now)[-1]
    assert today["attempts"] == 2
    assert today["low_conf"] == 1
    assert today["switches"] == 1


def test_add_noop_when_all_zero():
    store = TranslationTrendStore(":memory:")
    now = time.time()
    store.add(now=now)  # 全零 → 不写
    assert store.daily(days=1, now=now)[-1]["attempts"] == 0


def test_record_is_noop_until_configured():
    reset_translation_trend_store()
    # 未配置 → 恒 no-op、无 store
    record_translation_trend(attempts=5)
    assert get_translation_trend_store() is None
    # 配置后才落库
    store = configure_translation_trend_store(enabled=True, db_path=":memory:")
    assert store is not None
    record_translation_trend(attempts=3, low_conf=1)
    today = store.daily(days=1)[-1]
    assert today["attempts"] == 3
    assert today["low_conf"] == 1
    reset_translation_trend_store()


def test_prune_drops_old_days():
    store = TranslationTrendStore(":memory:")
    now = time.time()
    store.add(attempts=1, now=now - 200 * 86400)
    store.add(attempts=1, now=now)
    dropped = store.prune(retention_days=90, now=now)
    assert dropped == 1


@pytest.mark.asyncio
async def test_router_writes_trend_when_configured():
    # S：开启趋势落库后，低置信切换走过的 translate 应落 attempts/low_conf/switches
    from src.ai.translation_engines import EngineRouter
    from tests.test_translation_confidence import _FakeEngine

    reset_translation_trend_store()
    store = configure_translation_trend_store(enabled=True, db_path=":memory:")
    assert store is not None
    primary = _FakeEngine("primary", "我想你了")   # 未翻译（低置信）
    backup = _FakeEngine("backup", "君が恋しい")    # 合格日译
    router = EngineRouter([primary, backup], min_confidence=0.5)
    await router.translate("我想你了", source_lang="zh", target_lang="ja")
    today = store.daily(days=1)[-1]
    assert today["attempts"] >= 1
    assert today["low_conf"] >= 1
    assert today["switches"] >= 1
    reset_translation_trend_store()
