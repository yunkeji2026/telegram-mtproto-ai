"""P4-B：TTS 成本按日落库 store + pipeline 旁路 + 读端点 单测。"""
from __future__ import annotations

import asyncio


# ── TtsCostStore 日聚合 ───────────────────────────────────────────────────────
def test_store_record_and_daily_rollup():
    from src.ai.tts_cost_store import TtsCostStore

    s = TtsCostStore(":memory:")
    now = 1_700_000_000.0     # 固定锚点（某日 00:xx UTC）
    # 同一天多次合成 → upsert 累加
    s.record("elevenlabs", ok=True, cost_usd=0.10, now=now)
    s.record("elevenlabs", ok=True, cost_usd=0.05, now=now)
    s.record("edge_tts", ok=True, cost_usd=0.0, now=now)
    s.record("elevenlabs", ok=False, now=now)   # 失败也计 calls
    s.record("", cache_hit=True, now=now)
    s.record("", cache_hit=True, now=now)

    days = s.daily(days=1, now=now)
    assert len(days) == 1
    d = days[0]
    assert d["calls"] == 4
    assert d["ok"] == 3
    assert d["fail"] == 1
    assert round(d["cost_usd"], 4) == 0.15
    assert d["cache_hits"] == 2
    assert d["by_provider"]["elevenlabs"]["calls"] == 3
    assert round(d["by_provider"]["elevenlabs"]["cost_usd"], 4) == 0.15
    assert d["by_provider"]["edge_tts"]["calls"] == 1


def test_store_daily_fills_missing_days_with_zero():
    from src.ai.tts_cost_store import TtsCostStore

    s = TtsCostStore(":memory:")
    now = 1_700_000_000.0
    # 仅「3 天前」有数据
    s.record("edge_tts", ok=True, cost_usd=0.0, now=now - 3 * 86400)

    days = s.daily(days=7, now=now)
    assert len(days) == 7                       # 连续 7 天，无断点
    assert [x["calls"] for x in days].count(1) == 1   # 仅一天有 1 次
    assert sum(x["calls"] for x in days) == 1
    # 升序：最后一个是今天
    assert days[-1]["calls"] == 0


def test_store_prune_drops_old():
    from src.ai.tts_cost_store import TtsCostStore

    s = TtsCostStore(":memory:")
    now = 1_700_000_000.0
    s.record("edge_tts", ok=True, now=now - 100 * 86400)   # 100 天前
    s.record("edge_tts", ok=True, now=now)                  # 今天
    removed = s.prune(retention_days=90, now=now)
    assert removed >= 1
    days = s.daily(days=120, now=now)
    assert sum(x["calls"] for x in days) == 1               # 旧的已删，只剩今天


# ── 模块级开关：默认关 → record 恒 no-op ──────────────────────────────────────
def test_record_noop_when_disabled():
    from src.ai import tts_cost_store as tcs

    tcs.reset_tts_cost_store()
    # 未 configure → record 不抛、不建库
    tcs.record_tts_cost("elevenlabs", ok=True, cost_usd=1.0)
    assert tcs.get_tts_cost_store() is None


def test_configure_enables_and_records():
    from src.ai import tts_cost_store as tcs

    tcs.reset_tts_cost_store()
    store = tcs.configure_tts_cost_store(enabled=True, db_path=":memory:")
    assert store is not None
    assert tcs.get_tts_cost_store() is store
    tcs.record_tts_cost("elevenlabs", ok=True, cost_usd=0.2)
    tcs.record_tts_cost("", cache_hit=True)
    days = store.daily(days=1)
    assert days[-1]["calls"] == 1
    assert days[-1]["cache_hits"] == 1
    tcs.reset_tts_cost_store()


def test_configure_disabled_keeps_noop():
    from src.ai import tts_cost_store as tcs

    tcs.reset_tts_cost_store()
    tcs.configure_tts_cost_store(enabled=False, db_path=":memory:")
    tcs.record_tts_cost("elevenlabs", ok=True, cost_usd=1.0)
    assert tcs.get_tts_cost_store() is None
    tcs.reset_tts_cost_store()


# ── pipeline 旁路：合成后写入日聚合 ───────────────────────────────────────────
def test_pipeline_records_to_cost_store(tmp_path, monkeypatch):
    from src.ai.tts_pipeline import TTSPipeline, reset_tts_cache
    from src.ai.provider_stats import get_provider_stats
    from src.ai import tts_cost_store as tcs

    reset_tts_cache()
    get_provider_stats("tts", "tts").reset()
    tcs.reset_tts_cost_store()
    store = tcs.configure_tts_cost_store(enabled=True, db_path=":memory:")

    async def fake_edge(self, text, out, voice, spec=None):
        out.write_bytes(b"ID3edge" + b"\x00" * 600)

    monkeypatch.setattr(TTSPipeline, "_edge_tts", fake_edge)

    async def run():
        p = TTSPipeline({
            "enabled": True, "backend": "edge_tts",
            "format": "mp3", "out_dir": str(tmp_path),
            "cost_per_1k_chars": {"edge_tts": 0.0},
        })
        await p.synthesize("你好世界")
        days = store.daily(days=1)
        assert days[-1]["calls"] == 1
        assert days[-1]["by_provider"].get("edge_tts", {}).get("calls") == 1

    asyncio.run(run())
    reset_tts_cache()
    get_provider_stats("tts", "tts").reset()
    tcs.reset_tts_cost_store()


# ── /api/admin/tts-cost-trend 读端点 ─────────────────────────────────────────
def test_cost_trend_endpoint_disabled_returns_empty():
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from src.ai import tts_cost_store as tcs

    tcs.reset_tts_cost_store()   # 未开启
    app = _make_ops_app()
    client = TestClient(app, raise_server_exceptions=True)
    d = client.get("/api/admin/tts-cost-trend?days=7").json()
    assert d["enabled"] is False
    assert d["days"] == []


def test_cost_trend_endpoint_returns_series():
    from fastapi.testclient import TestClient
    from src.ai import tts_cost_store as tcs

    tcs.reset_tts_cost_store()
    store = tcs.configure_tts_cost_store(enabled=True, db_path=":memory:")
    store.record("elevenlabs", ok=True, cost_usd=0.5)
    app = _make_ops_app()
    client = TestClient(app, raise_server_exceptions=True)
    d = client.get("/api/admin/tts-cost-trend?days=7").json()
    assert d["enabled"] is True
    assert len(d["days"]) == 7
    assert round(d["days"][-1]["cost_usd"], 4) == 0.5
    tcs.reset_tts_cost_store()


def _make_ops_app():
    """最小 app：仅挂 tts-cost-trend 端点（api_auth 放行）。"""
    from fastapi import FastAPI
    from src.web.routes.ops_overview_routes import register_ops_overview_routes

    app = FastAPI()

    class _Ctx:
        def api_auth(self, request):
            return True

        def api_write(self, perm):
            def _dep():
                return True
            return _dep

        def page_auth(self, request):
            return True

        templates = None
        config_manager = None
        audit_store = None
        user_store = None
        token = None

    register_ops_overview_routes(app, _Ctx())
    return app
