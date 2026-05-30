"""B1：漏斗时序快照 —— store 重放 + API + 前端组件结构测试。

测试焦点：

- store.count_stage_transitions_by_day 正确从 journey_events 重放
- 同 journey 一天内反复同 stage 进入只算 1 次（DISTINCT）
- silence_decay 事件**不**计入（业务事件 vs 后台衰减）
- channel 过滤 + DISTINCT JOIN（防 1 journey 多 CI 重复计数）
- days 边界（1 / 30 / 365）+ 非法值 → 400
- API /api/funnel/timeseries 返回正确结构
- 前端 partial 暴露 _refreshTimeseries / _renderTimeseries / TS_LINES
- 前端 inline SVG 渲染零外部依赖
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.contacts.gateway import ContactGateway
from src.contacts.handoff import HandoffTokenService
from src.contacts.merge import MergeService
from src.contacts.models import (
    CHANNEL_LINE,
    CHANNEL_MESSENGER,
    STAGE_ENGAGED,
    STAGE_HANDOFF_SENT,
    STAGE_INITIAL,
    STAGE_LINE_ADDED,
)
from src.contacts.store import ContactStore
from src.web.routes.contacts_routes import register_contacts_routes


# ════════════════════════════════════════════════════════════════════════
# fixtures
# ════════════════════════════════════════════════════════════════════════


@pytest.fixture
def client(tmp_path):
    from src.web.routes.contacts_routes import _intimacy_trend_cache_clear
    _intimacy_trend_cache_clear()
    store = ContactStore(db_path=tmp_path / "contacts.db")
    handoff = HandoffTokenService(store, ttl_seconds=3600)
    merge = MergeService(store)
    gateway = ContactGateway(store, handoff, merge)
    app = FastAPI()
    register_contacts_routes(
        app, api_auth=lambda: None,
        contacts_store=store, merge_service=merge, gateway=gateway,
    )
    tc = TestClient(app)
    tc.store = store        # type: ignore[attr-defined]
    tc.gateway = gateway    # type: ignore[attr-defined]
    yield tc
    store.close()


def _utc_ts(y, m, d, h=12):
    """构造 UTC 时间戳，方便在测试里"模拟某一天发生了事件"。"""
    return int(datetime(y, m, d, h, tzinfo=timezone.utc).timestamp())


# ════════════════════════════════════════════════════════════════════════
# store 层：count_stage_transitions_by_day
# ════════════════════════════════════════════════════════════════════════


class TestStoreTimeseriesBase:
    def test_empty_store_returns_zero_filled_days(self, client):
        """空 events 表 → 返回 N 天空数据（不是空 list，前端折线不断点）。"""
        result = client.store.count_stage_transitions_by_day(days=7)
        assert len(result) == 7
        assert all(item["by_stage"] == {} for item in result)
        # day 字段都是 YYYY-MM-DD 格式
        for item in result:
            assert len(item["day"]) == 10
            assert item["day"][4] == '-' and item["day"][7] == '-'

    def test_days_ascending_order(self, client):
        """series 必须按 day 升序排列（前端折线图依赖此顺序）。"""
        result = client.store.count_stage_transitions_by_day(days=10)
        days = [item["day"] for item in result]
        assert days == sorted(days)

    def test_days_clamped_to_at_least_one(self, client):
        """days=0 / 负数 → clamp 到 1。"""
        result = client.store.count_stage_transitions_by_day(days=0)
        assert len(result) == 1
        result = client.store.count_stage_transitions_by_day(days=-5)
        assert len(result) == 1


class TestStoreTimeseriesReplay:
    """从 journey_events 真实重放的核心逻辑。"""

    def _seed_stage_change(self, store, journey_id, from_s, to_s, ts):
        """直接 append_event，模拟 FSM transit 时落 stage_change 事件。"""
        store.append_event(
            journey_id=journey_id,
            event_type="stage_change",
            payload={"from": from_s, "to": to_s},
        )
        # append_event 用 self._now() 写入 ts；我们手动改成指定 ts 以模拟历史
        with store._lock:
            store._conn.execute(
                "UPDATE journey_events SET ts=? "
                "WHERE journey_id=? AND event_type='stage_change' "
                "AND ts=(SELECT MAX(ts) FROM journey_events "
                "       WHERE journey_id=? AND event_type='stage_change')",
                (ts, journey_id, journey_id),
            )
            store._conn.commit()

    def test_aggregates_by_day_and_stage(self, client):
        ctx = client.gateway.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="u1")
        jid = ctx.journey.journey_id
        # 在 3 天里产生 stage_change 事件
        now = _utc_ts(2026, 5, 18)
        self._seed_stage_change(client.store, jid, "INITIAL", "ENGAGED",
                                _utc_ts(2026, 5, 16))
        self._seed_stage_change(client.store, jid, "ENGAGED", "HANDOFF_SENT",
                                _utc_ts(2026, 5, 17))
        self._seed_stage_change(client.store, jid, "HANDOFF_SENT", "LINE_ADDED",
                                _utc_ts(2026, 5, 18))

        result = client.store.count_stage_transitions_by_day(
            days=5, now_ts=now)
        # 5 天 = 5/14..5/18，找出含数据的天
        day_map = {item["day"]: item["by_stage"] for item in result}
        assert day_map["2026-05-16"].get("ENGAGED") == 1
        assert day_map["2026-05-17"].get("HANDOFF_SENT") == 1
        assert day_map["2026-05-18"].get("LINE_ADDED") == 1
        # 没数据的天保持空 dict
        assert day_map["2026-05-15"] == {}

    def test_same_journey_same_day_same_stage_counted_once(self, client):
        """同一 journey 一天内反复进入同 stage（flip-flop）只算 1 次——
        防止后台抖动让流量数膨胀。
        """
        ctx = client.gateway.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="u1")
        jid = ctx.journey.journey_id
        # 同 journey 同天 5 次 stage_change 都去 ENGAGED
        for _ in range(5):
            self._seed_stage_change(client.store, jid, "INITIAL", "ENGAGED",
                                    _utc_ts(2026, 5, 18))
        result = client.store.count_stage_transitions_by_day(
            days=3, now_ts=_utc_ts(2026, 5, 18))
        # 应该是 1 不是 5
        day_map = {item["day"]: item["by_stage"] for item in result}
        assert day_map["2026-05-18"].get("ENGAGED") == 1

    def test_silence_decay_excluded(self, client):
        """silence_decay 是后台衰减，**不**计入业务流量。"""
        ctx = client.gateway.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="u1")
        jid = ctx.journey.journey_id
        # 落 silence_decay 事件（模拟 journey_fsm._silence_decay）
        client.store.append_event(
            journey_id=jid,
            event_type="silence_decay",
            payload={"from": "ENGAGED", "to": "INITIAL"},
        )
        # 同时落一个 stage_change 对照
        self._seed_stage_change(client.store, jid, "INITIAL", "ENGAGED",
                                _utc_ts(2026, 5, 18))

        result = client.store.count_stage_transitions_by_day(
            days=3, now_ts=_utc_ts(2026, 5, 18))
        day_map = {item["day"]: item["by_stage"] for item in result}
        # stage_change 算了 1 次（ENGAGED），silence_decay 不应该出现
        # （它会让 INITIAL 多 1 而不该出现）
        assert day_map["2026-05-18"].get("ENGAGED") == 1
        # INITIAL 不应该被 silence_decay 加上
        assert day_map["2026-05-18"].get("INITIAL", 0) == 0


class TestStoreTimeseriesChannelFilter:
    def _seed(self, store, journey_id, to_s, ts):
        store.append_event(
            journey_id=journey_id,
            event_type="stage_change",
            payload={"from": "X", "to": to_s},
        )
        with store._lock:
            store._conn.execute(
                "UPDATE journey_events SET ts=? "
                "WHERE journey_id=? AND event_type='stage_change' "
                "AND ts=(SELECT MAX(ts) FROM journey_events "
                "       WHERE journey_id=? AND event_type='stage_change')",
                (ts, journey_id, journey_id),
            )
            store._conn.commit()

    def test_filter_by_channel(self, client):
        ctx_m = client.gateway.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="m1")
        ctx_l = client.gateway.on_peer_seen(
            channel=CHANNEL_LINE, account_id="a", external_id="l1")
        ts = _utc_ts(2026, 5, 18)
        self._seed(client.store, ctx_m.journey.journey_id, "ENGAGED", ts)
        self._seed(client.store, ctx_l.journey.journey_id, "ENGAGED", ts)

        # 不带 channel → 看到 2 个 ENGAGED
        result_all = client.store.count_stage_transitions_by_day(
            days=2, now_ts=ts)
        day_map = {i["day"]: i["by_stage"] for i in result_all}
        assert day_map["2026-05-18"].get("ENGAGED") == 2

        # channel=line → 只看到 LINE 的 1 个
        result_l = client.store.count_stage_transitions_by_day(
            days=2, channel=CHANNEL_LINE, now_ts=ts)
        day_map_l = {i["day"]: i["by_stage"] for i in result_l}
        assert day_map_l["2026-05-18"].get("ENGAGED") == 1

    def test_distinct_prevents_double_count_after_merge(self, client):
        """同 journey 在同 channel 多个 CI（合并后场景）—— DISTINCT 防重复。"""
        ctx_a = client.gateway.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="pa", external_id="alice_a")
        ctx_b = client.gateway.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="pb", external_id="alice_b")
        # 合并：ctx_b 的 CI 迁到 ctx_a 的 contact
        client.store.relink_channel_identity(
            ci_id=ctx_b.channel_identity.channel_identity_id,
            new_contact_id=ctx_a.contact.contact_id,
            linked_via="merge", attribution_confidence=1.0,
        )
        # ctx_a 的 journey 现在有 2 个 messenger CI
        ts = _utc_ts(2026, 5, 18)
        self._seed(client.store, ctx_a.journey.journey_id, "ENGAGED", ts)

        result = client.store.count_stage_transitions_by_day(
            days=2, channel=CHANNEL_MESSENGER, now_ts=ts)
        day_map = {i["day"]: i["by_stage"] for i in result}
        # 应该还是 1，不是 2（DISTINCT 保护）
        assert day_map["2026-05-18"].get("ENGAGED") == 1


# ════════════════════════════════════════════════════════════════════════
# API: /api/funnel/timeseries
# ════════════════════════════════════════════════════════════════════════


class TestApiTimeseries:
    def test_default_30_days(self, client):
        r = client.get("/api/funnel/timeseries")
        assert r.status_code == 200
        body = r.json()
        assert body["days"] == 30
        assert body["scope"] == "all"
        assert len(body["series"]) == 30

    def test_custom_days(self, client):
        r = client.get("/api/funnel/timeseries?days=7")
        assert r.status_code == 200
        assert r.json()["days"] == 7
        assert len(r.json()["series"]) == 7

    def test_days_clamped_to_max_365(self, client):
        """days > 365 应当 400，避免一次性扫整张 events 表炸性能。"""
        r = client.get("/api/funnel/timeseries?days=10000")
        assert r.status_code == 400
        assert "1..365" in r.json()["detail"]

    def test_days_too_small(self, client):
        r = client.get("/api/funnel/timeseries?days=0")
        assert r.status_code == 400

    def test_invalid_channel_returns_400(self, client):
        r = client.get("/api/funnel/timeseries?channel=facebook")
        assert r.status_code == 400
        assert "facebook" in r.json()["detail"]

    def test_series_structure(self, client):
        """series 必须含 day / by_stage / rates 字段，rates 含 4 个 key。"""
        # 制造一点数据
        ctx = client.gateway.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="u1",
            direction="in", text_preview="hi")
        r = client.get("/api/funnel/timeseries?days=3")
        body = r.json()
        item = body["series"][-1]  # 今天那项最可能有数据
        assert "day" in item
        assert "by_stage" in item
        assert "rates" in item
        assert set(item["rates"].keys()) == {
            "engaged_rate", "handoff_rate", "line_add_rate", "bonded_rate",
        }

    def test_channel_scope_reflected(self, client):
        r = client.get(f"/api/funnel/timeseries?channel={CHANNEL_LINE}&days=3")
        assert r.status_code == 200
        assert r.json()["scope"] == CHANNEL_LINE


# ════════════════════════════════════════════════════════════════════════
# 前端 partial 结构
# ════════════════════════════════════════════════════════════════════════


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "src" / "web" / "templates"


@pytest.fixture(scope="module")
def partial_text() -> str:
    return (TEMPLATES_DIR / "_rpa_shared_funnel.html").read_text(encoding="utf-8")


class TestFunnelTimeseriesPartial:
    @pytest.mark.parametrize("hook", [
        "rpa-funnel-ts",          # 容器 wrap
        "rpa-funnel-ts-days",     # 7d/30d 按钮区
        "rpa-funnel-ts-body",     # SVG 容器
        "rpa-funnel-ts-svg",      # SVG class
        "rpa-funnel-ts-legend",   # legend
        "rpa-funnel-ts-empty",    # 空态文案
    ])
    def test_partial_has_required_dom_ids(self, partial_text: str, hook: str):
        assert hook in partial_text, f"_rpa_shared_funnel.html 必须含 {hook}"

    @pytest.mark.parametrize("api", [
        "_refreshTimeseries",
        "_renderTimeseries",
        "_bindTsControls",
        "_tsDays",
        "_tsLineVisible",
        "TS_LINES",
    ])
    def test_partial_exposes_required_js(self, partial_text: str, api: str):
        assert api in partial_text, f"partial 必须定义/引用 {api}"

    def test_calls_timeseries_endpoint(self, partial_text: str):
        assert "/api/funnel/timeseries" in partial_text

    def test_4_lines_engaged_handoff_line_added_bonded(self, partial_text: str):
        """4 条折线的 key 必须与 API 返回的 rates 字段对齐。"""
        for key in ["engaged_rate", "handoff_rate", "line_add_rate", "bonded_rate"]:
            assert f"'{key}'" in partial_text, (
                f"TS_LINES 必须含 {key}（与 /api/funnel/timeseries rates 对齐）"
            )

    def test_inline_svg_no_third_party_lib(self, partial_text: str):
        """折线图必须是 inline SVG，不引入 Chart.js / ApexCharts 等。"""
        # 关键证据：手写 path / circle / text 标签
        assert "<path" in partial_text or "'<path" in partial_text or "`<path" in partial_text
        # 防御性：不应该有第三方库 import
        for lib in ["chart.js", "Chart.js", "apexcharts", "d3.js", "echarts"]:
            assert lib not in partial_text, (
                f"partial 不应该引入 {lib}，inline SVG 已足够"
            )

    def test_supports_7d_and_30d_window(self, partial_text: str):
        assert 'data-days="7"' in partial_text
        assert 'data-days="30"' in partial_text

    def test_handles_empty_series_gracefully(self, partial_text: str):
        """series=[] 时必须显示"暂无时序数据"而不是抛 JS 错误。"""
        assert "暂无时序数据" in partial_text

    def test_null_rate_creates_path_break(self, partial_text: str):
        """rates[key]=null 时折线必须断点（segStart 重置），不要画"飞过去"的假数据线。"""
        assert "segStart" in partial_text or "segStart = true" in partial_text

    def test_refresh_propagates_scope_to_timeseries(self, partial_text: str):
        """切 channel chip 时，时序图也要带新的 channel param 重新拉。

        关键证据：F._refreshTimeseries 用 F._scope 拼 URL
        """
        # _refreshTimeseries 函数体内必须引用 F._scope
        idx = partial_text.find("F._refreshTimeseries = function")
        assert idx > 0, "找不到 _refreshTimeseries 函数定义"
        body = partial_text[idx:idx + 1200]
        assert "F._scope" in body, (
            "_refreshTimeseries 必须用 F._scope 拼 channel param"
        )
