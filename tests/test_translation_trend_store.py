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
    assert today["sem_low"] == 0   # 确定性低置信 ≠ 语义闸门命中
    reset_translation_trend_store()


def test_sem_low_column_accumulates_and_rates():
    store = TranslationTrendStore(":memory:")
    now = time.time()
    store.add(attempts=10, low_conf=3, sem_low=2, now=now)
    store.add(sem_low=1, now=now)   # 只有 sem_low 增量也应落库（非全零）
    today = store.daily(days=1, now=now)[-1]
    assert today["sem_low"] == 3
    assert today["sem_low_rate"] == 0.3


def test_sem_low_migration_on_existing_db(tmp_path):
    """旧库（无 sem_low 列）被新代码打开 → ALTER 迁移后可读写 sem_low。"""
    import sqlite3

    db = tmp_path / "xlate_trend.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE xlate_trend_daily ("
        " day TEXT NOT NULL PRIMARY KEY,"
        " attempts INTEGER NOT NULL DEFAULT 0,"
        " low_conf INTEGER NOT NULL DEFAULT 0,"
        " switches INTEGER NOT NULL DEFAULT 0)")
    conn.execute("INSERT INTO xlate_trend_daily VALUES ('2026-01-01', 5, 1, 1)")
    conn.commit()
    conn.close()

    store = TranslationTrendStore(db)
    now = time.time()
    store.add(attempts=2, sem_low=1, now=now)
    today = store.daily(days=1, now=now)[-1]
    assert today["sem_low"] == 1
    # 旧行 sem_low 补 0（列默认值）
    old = [r for r in store.daily(days=365 * 2, now=now) if r["day"] == "2026-01-01"]
    if old:   # 超出窗口则跳过旧行断言（窗口上限 90 天）
        assert old[0]["sem_low"] == 0


@pytest.mark.asyncio
async def test_router_semantic_low_writes_sem_low():
    """语义闸门命中 → trend 落 sem_low（与确定性 low_conf 区分开）。"""
    from src.ai.translation_engines import EngineRouter
    from tests.test_translation_confidence import _FakeEngine

    reset_translation_trend_store()
    store = configure_translation_trend_store(enabled=True, db_path=":memory:")
    primary = _FakeEngine("primary", "The weather is nice today")   # 确定性达标
    backup = _FakeEngine("backup", "I really miss you my dear")

    async def _embed(texts):
        # 主引擎译文与源文语义相反（余弦≈-1 → 低于阈值）；备引擎相同向量（达标）
        src = [1.0, 0.0]
        return [src, [-1.0, 0.0] if "weather" in texts[1] else [1.0, 0.0]]

    router = EngineRouter([primary, backup], min_confidence=0.3,
                          semantic_embed_fn=_embed, semantic_min_similarity=0.65)
    out = await router.translate("我今天特别想你，想到睡不着", source_lang="zh",
                                 target_lang="en")
    assert "miss you" in out.text
    today = store.daily(days=1)[-1]
    assert today["sem_low"] >= 1
    assert today["sem_low_rate"] > 0
    reset_translation_trend_store()
