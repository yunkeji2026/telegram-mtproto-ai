"""B2: KPI 漏斗告警 —— 检测逻辑 + store 方法 + API 端点 + 前端结构测试。

测试焦点:
  - detect_kpi_drops 纯函数：冷启动保护 / 量保护 / 双重门槛 / critical 升级
  - ContactStore: insert_kpi_alert 去重 / list / ack / ack_all / count_unacked
  - API: GET /api/funnel/alerts / POST ack / POST ack-all
  - bootstrap: _run_kpi_alert_once 集成（不含 asyncio loop）
  - 前端: HTML/JS 结构暴露正确函数
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.contacts.kpi_alerting import detect_kpi_drops, _DEFAULT_THRESHOLDS, _KPI_DEFS
from src.contacts.store import ContactStore
from src.contacts.gateway import ContactGateway
from src.contacts.handoff import HandoffTokenService
from src.contacts.merge import MergeService
from src.web.routes.contacts_routes import register_contacts_routes


# ════════════════════════════════════════════════════════════════════════
# helpers
# ════════════════════════════════════════════════════════════════════════

def _make_series(
    n_days: int = 10,
    *,
    engaged_rate: Optional[float] = 60.0,
    handoff_rate: Optional[float] = 50.0,
    line_add_rate: Optional[float] = 40.0,
    bonded_rate: Optional[float] = 30.0,
    today_engaged: Optional[float] = None,
    today_handoff: Optional[float] = None,
    today_line_add: Optional[float] = None,
    today_bonded: Optional[float] = None,
    volume: int = 10,
) -> List[Dict[str, Any]]:
    """生成 n_days 天的 series，今天（最后一项）的率可单独 override。"""
    series = []
    for i in range(n_days):
        is_today = (i == n_days - 1)
        series.append({
            "day": f"2026-05-{i+1:02d}",
            "by_stage": {
                "INITIAL": volume,
                "ENGAGED": volume,
                "HANDOFF_SENT": volume,
                "LINE_ADDED": volume,
            },
            "rates": {
                "engaged_rate": (today_engaged if is_today and today_engaged is not None
                                 else engaged_rate),
                "handoff_rate": (today_handoff if is_today and today_handoff is not None
                                 else handoff_rate),
                "line_add_rate": (today_line_add if is_today and today_line_add is not None
                                  else line_add_rate),
                "bonded_rate": (today_bonded if is_today and today_bonded is not None
                                else bonded_rate),
            },
        })
    return series


@pytest.fixture
def store(tmp_path):
    return ContactStore(db_path=tmp_path / "contacts.db")


@pytest.fixture
def client(tmp_path):
    from src.web.routes.contacts_routes import _intimacy_trend_cache_clear
    _intimacy_trend_cache_clear()
    s = ContactStore(db_path=tmp_path / "contacts.db")
    handoff = HandoffTokenService(s, ttl_seconds=3600)
    merge = MergeService(s)
    gateway = ContactGateway(s, handoff, merge)
    app = FastAPI()
    register_contacts_routes(
        app, api_auth=lambda: None,
        contacts_store=s, merge_service=merge, gateway=gateway,
    )
    return TestClient(app), s


# ════════════════════════════════════════════════════════════════════════
# detect_kpi_drops — 纯函数测试
# ════════════════════════════════════════════════════════════════════════

class TestDetectKpiDrops:
    def test_empty_series_returns_empty(self):
        assert detect_kpi_drops([]) == []

    def test_single_item_returns_empty(self):
        assert detect_kpi_drops(_make_series(1)) == []

    def test_cold_start_fewer_than_min_days_skipped(self):
        """历史数据不足 min_days_required（默认 5）时不告警。"""
        series = _make_series(5, today_engaged=10.0)
        # window = series[-8:-1] = series[0:4] = 4 items，< 5 → skip
        alerts = detect_kpi_drops(series)
        assert alerts == []

    def test_sufficient_history_triggers_alert(self):
        """足够历史且今日大幅下跌时生成告警。"""
        series = _make_series(9, engaged_rate=60.0, today_engaged=10.0)
        alerts = detect_kpi_drops(series)
        kinds = [a["kind"] for a in alerts]
        assert "kpi_drop_engaged_rate" in kinds

    def test_no_alert_when_today_above_abs_floor(self):
        """今日值高于 abs_floor_pct（80%）时不告警，即使下跌了。"""
        series = _make_series(9, engaged_rate=95.0, today_engaged=81.0)
        alerts = detect_kpi_drops(series)
        assert not any(a["kind"] == "kpi_drop_engaged_rate" for a in alerts)

    def test_no_alert_when_drop_below_threshold(self):
        """下跌幅度 < drop_pct_threshold（默认 30%）时不告警。"""
        # avg=60, today=50 → drop=(60-50)/60*100=16.7% < 30%
        series = _make_series(9, engaged_rate=60.0, today_engaged=50.0)
        alerts = detect_kpi_drops(series)
        assert not any(a["kind"] == "kpi_drop_engaged_rate" for a in alerts)

    def test_critical_severity_at_double_threshold(self):
        """下跌 >= 2×drop_pct_threshold 时升级为 critical。"""
        # avg=60, today=6 → drop=90% >= 60%
        series = _make_series(9, engaged_rate=60.0, today_engaged=6.0)
        alerts = detect_kpi_drops(series)
        found = [a for a in alerts if a["kind"] == "kpi_drop_engaged_rate"]
        assert found
        assert found[0]["severity"] == "critical"

    def test_warn_severity_below_double_threshold(self):
        """下跌 < 2×threshold 时为 warn。"""
        # avg=60, today=30 → drop=50% 在 [30%, 60%) 区间
        series = _make_series(9, engaged_rate=60.0, today_engaged=30.0)
        alerts = detect_kpi_drops(series)
        found = [a for a in alerts if a["kind"] == "kpi_drop_engaged_rate"]
        assert found
        assert found[0]["severity"] == "warn"

    def test_volume_protection_skips_low_volume(self):
        """当日分母量 < min_daily_volume（默认 3）时跳过。"""
        series = _make_series(9, engaged_rate=60.0, today_engaged=10.0)
        # 把今天的 INITIAL 量改为 2（< 3）
        series[-1]["by_stage"]["INITIAL"] = 2
        alerts = detect_kpi_drops(series)
        assert not any(a["kind"] == "kpi_drop_engaged_rate" for a in alerts)

    def test_null_today_value_skipped(self):
        """今天的率为 None（分母为 0）时跳过。"""
        series = _make_series(9, engaged_rate=60.0, today_engaged=None)
        series[-1]["rates"]["engaged_rate"] = None
        alerts = detect_kpi_drops(series)
        assert not any(a["kind"] == "kpi_drop_engaged_rate" for a in alerts)

    def test_custom_thresholds_respected(self):
        """自定义 thresholds 覆盖默认值。"""
        series = _make_series(9, engaged_rate=60.0, today_engaged=45.0)
        # 默认 30% 下不告警（drop=25%）；降到 20% 后才告警
        assert detect_kpi_drops(series, thresholds={"drop_pct_threshold": 20.0}) != []
        assert detect_kpi_drops(series) == []

    def test_detail_fields_present(self):
        """告警 detail 包含 rate_key / today_val / avg_7d / drop_pct 等。"""
        series = _make_series(9, engaged_rate=60.0, today_engaged=10.0)
        alerts = detect_kpi_drops(series)
        found = next(a for a in alerts if a["kind"] == "kpi_drop_engaged_rate")
        d = found["detail"]
        assert d["rate_key"] == "engaged_rate"
        assert d["today_val"] == 10.0
        assert d["avg_7d"] == pytest.approx(60.0, abs=1)
        assert d["drop_pct"] > 80.0
        assert d["today_volume"] > 0
        assert d["history_days"] >= 5

    def test_all_four_kpi_keys_detectable(self):
        """4 个 KPI 各自独立检测。"""
        series = _make_series(
            9,
            engaged_rate=60.0, handoff_rate=50.0,
            line_add_rate=40.0, bonded_rate=30.0,
            today_engaged=5.0, today_handoff=5.0,
            today_line_add=5.0, today_bonded=5.0,
        )
        alerts = detect_kpi_drops(series)
        kinds = {a["kind"] for a in alerts}
        assert "kpi_drop_engaged_rate" in kinds
        assert "kpi_drop_handoff_rate" in kinds
        assert "kpi_drop_line_add_rate" in kinds
        assert "kpi_drop_bonded_rate" in kinds

    def test_stable_kpi_no_alert(self):
        """KPI 稳定时不告警。"""
        series = _make_series(9, engaged_rate=60.0)
        alerts = detect_kpi_drops(series)
        assert alerts == []


# ════════════════════════════════════════════════════════════════════════
# ContactStore KPI alert 方法测试
# ════════════════════════════════════════════════════════════════════════

class TestStoreKpiAlerts:
    def test_insert_returns_id(self, store):
        aid = store.insert_kpi_alert(kind="kpi_drop_engaged_rate", message="test")
        assert isinstance(aid, int) and aid > 0

    def test_dedup_within_window(self, store):
        store.insert_kpi_alert(kind="kpi_drop_x", dedup_window_sec=3600)
        aid2 = store.insert_kpi_alert(kind="kpi_drop_x", dedup_window_sec=3600)
        assert aid2 is None

    def test_no_dedup_after_window(self, store):
        store.insert_kpi_alert(kind="kpi_drop_y", dedup_window_sec=0.001)
        time.sleep(0.01)
        aid2 = store.insert_kpi_alert(kind="kpi_drop_y", dedup_window_sec=0.001)
        assert aid2 is not None

    def test_different_kinds_not_deduped(self, store):
        a1 = store.insert_kpi_alert(kind="kpi_drop_a")
        a2 = store.insert_kpi_alert(kind="kpi_drop_b")
        assert a1 is not None and a2 is not None

    def test_list_returns_inserted(self, store):
        store.insert_kpi_alert(kind="kpi_drop_engaged_rate", message="msg1")
        rows = store.list_kpi_alerts()
        assert len(rows) == 1
        assert rows[0]["kind"] == "kpi_drop_engaged_rate"
        assert rows[0]["message"] == "msg1"
        assert rows[0]["acked"] is False

    def test_list_unacked_only(self, store):
        aid = store.insert_kpi_alert(kind="k1")
        store.insert_kpi_alert(kind="k2", dedup_window_sec=0)
        store.ack_kpi_alert(aid)
        rows = store.list_kpi_alerts(unacked_only=True)
        assert all(not r["acked"] for r in rows)

    def test_list_limit(self, store):
        for i in range(5):
            store.insert_kpi_alert(kind=f"kpi_{i}", dedup_window_sec=0)
        assert len(store.list_kpi_alerts(limit=3)) == 3

    def test_ack_single(self, store):
        aid = store.insert_kpi_alert(kind="k1")
        assert store.ack_kpi_alert(aid, acked_by="admin") is True
        rows = store.list_kpi_alerts()
        assert rows[0]["acked"] is True
        assert rows[0]["acked_by"] == "admin"
        assert rows[0]["acked_at"] is not None

    def test_ack_already_acked_returns_false(self, store):
        aid = store.insert_kpi_alert(kind="k1")
        store.ack_kpi_alert(aid)
        assert store.ack_kpi_alert(aid) is False

    def test_ack_nonexistent_returns_false(self, store):
        assert store.ack_kpi_alert(99999) is False

    def test_ack_all(self, store):
        for i in range(3):
            store.insert_kpi_alert(kind=f"k{i}", dedup_window_sec=0)
        n = store.ack_all_kpi_alerts(acked_by="ops")
        assert n == 3
        assert store.count_unacked_kpi_alerts() == 0

    def test_ack_all_returns_only_unacked_count(self, store):
        aid1 = store.insert_kpi_alert(kind="k1")
        store.insert_kpi_alert(kind="k2", dedup_window_sec=0)
        store.ack_kpi_alert(aid1)
        n = store.ack_all_kpi_alerts()
        assert n == 1  # only k2 was unacked

    def test_count_unacked(self, store):
        for i in range(4):
            store.insert_kpi_alert(kind=f"k{i}", dedup_window_sec=0)
        assert store.count_unacked_kpi_alerts() == 4
        store.ack_all_kpi_alerts()
        assert store.count_unacked_kpi_alerts() == 0

    def test_detail_json_roundtrip(self, store):
        detail = {"rate_key": "engaged_rate", "drop_pct": 45.3, "today_val": 12.0}
        aid = store.insert_kpi_alert(kind="k1", detail=detail)
        rows = store.list_kpi_alerts()
        assert rows[0]["detail"] == detail

    def test_list_order_newest_first(self, store):
        a1 = store.insert_kpi_alert(kind="k1")
        time.sleep(0.01)
        a2 = store.insert_kpi_alert(kind="k2", dedup_window_sec=0)
        rows = store.list_kpi_alerts()
        assert rows[0]["id"] == a2  # 最新在前


# ════════════════════════════════════════════════════════════════════════
# API endpoint 测试
# ════════════════════════════════════════════════════════════════════════

class TestFunnelAlertsApi:
    def test_list_empty(self, client):
        tc, _ = client
        r = tc.get("/api/funnel/alerts")
        assert r.status_code == 200
        d = r.json()
        assert d["items"] == []
        assert d["unacked_count"] == 0

    def test_list_with_alerts(self, client):
        tc, store = client
        store.insert_kpi_alert(kind="kpi_drop_engaged_rate", severity="warn",
                               message="互动率下跌")
        r = tc.get("/api/funnel/alerts")
        assert r.status_code == 200
        d = r.json()
        assert len(d["items"]) == 1
        assert d["unacked_count"] == 1
        item = d["items"][0]
        assert item["kind"] == "kpi_drop_engaged_rate"
        assert item["severity"] == "warn"
        assert item["message"] == "互动率下跌"
        assert item["acked"] is False

    def test_list_unacked_only(self, client):
        tc, store = client
        aid = store.insert_kpi_alert(kind="k1")
        store.insert_kpi_alert(kind="k2", dedup_window_sec=0)
        store.ack_kpi_alert(aid)
        r = tc.get("/api/funnel/alerts?unacked_only=true")
        assert r.status_code == 200
        d = r.json()
        assert all(not item["acked"] for item in d["items"])

    def test_list_limit_param(self, client):
        tc, store = client
        for i in range(5):
            store.insert_kpi_alert(kind=f"k{i}", dedup_window_sec=0)
        r = tc.get("/api/funnel/alerts?limit=3")
        assert r.status_code == 200
        assert len(r.json()["items"]) == 3

    def test_list_limit_clamp(self, client):
        tc, _ = client
        r = tc.get("/api/funnel/alerts?limit=9999")
        assert r.status_code == 200  # clamped to 200, not error

    def test_ack_single(self, client):
        tc, store = client
        aid = store.insert_kpi_alert(kind="kpi_drop_x")
        r = tc.post(f"/api/funnel/alerts/{aid}/ack")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert r.json()["alert_id"] == aid
        # verify via list
        items = tc.get("/api/funnel/alerts").json()["items"]
        assert items[0]["acked"] is True

    def test_ack_nonexistent_404(self, client):
        tc, _ = client
        r = tc.post("/api/funnel/alerts/99999/ack")
        assert r.status_code == 404

    def test_ack_all(self, client):
        tc, store = client
        for i in range(3):
            store.insert_kpi_alert(kind=f"k{i}", dedup_window_sec=0)
        r = tc.post("/api/funnel/alerts/ack-all")
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert d["acked_count"] == 3
        assert tc.get("/api/funnel/alerts").json()["unacked_count"] == 0

    def test_ack_all_empty_returns_zero(self, client):
        tc, _ = client
        r = tc.post("/api/funnel/alerts/ack-all")
        assert r.status_code == 200
        assert r.json()["acked_count"] == 0

    def test_unacked_count_decreases_after_ack(self, client):
        tc, store = client
        store.insert_kpi_alert(kind="k1")
        store.insert_kpi_alert(kind="k2", dedup_window_sec=0)
        assert tc.get("/api/funnel/alerts").json()["unacked_count"] == 2
        aid = store.list_kpi_alerts()[0]["id"]
        tc.post(f"/api/funnel/alerts/{aid}/ack")
        assert tc.get("/api/funnel/alerts").json()["unacked_count"] == 1


# ════════════════════════════════════════════════════════════════════════
# bootstrap._run_kpi_alert_once 集成测试（不含 asyncio loop）
# ════════════════════════════════════════════════════════════════════════

class TestBootstrapRunOnce:
    def _make_subsystem(self, store):
        from src.contacts.bootstrap import ContactsSubsystem
        from src.contacts.gateway import ContactGateway
        from src.contacts.handoff import HandoffTokenService
        from src.contacts.merge import MergeService
        hs = HandoffTokenService(store, ttl_seconds=3600)
        ms = MergeService(store)
        gw = ContactGateway(store, hs, ms)
        from src.contacts.rpa_hooks import GatewayContactHooks
        hooks = GatewayContactHooks(gw)
        return ContactsSubsystem(
            store=store, handoff_svc=hs, merge_svc=ms, gateway=gw, hooks=hooks,
            config_snapshot={},
        )

    def test_run_once_no_data_returns_zero(self, store):
        sub = self._make_subsystem(store)
        n = sub._run_kpi_alert_once()
        assert n == 0

    def test_run_once_deduplication(self, store):
        sub = self._make_subsystem(store)
        sub.config_snapshot = {"kpi_alert": {"dedup_window_sec": 3600}}
        n1 = sub._run_kpi_alert_once()
        n2 = sub._run_kpi_alert_once()
        assert n2 == 0  # 重复运行被去重

    def test_run_once_custom_config(self, store):
        """config_snapshot 里的 kpi_alert 节能被正确读取。"""
        sub = self._make_subsystem(store)
        sub.config_snapshot = {
            "kpi_alert": {
                "dedup_window_sec": 0.001,
                "thresholds": {"drop_pct_threshold": 99.9},  # 超高阈值，不会触发
            }
        }
        n = sub._run_kpi_alert_once()
        assert n == 0


# ════════════════════════════════════════════════════════════════════════
# 前端 HTML/JS 结构测试
# ════════════════════════════════════════════════════════════════════════

class TestFrontendAlertStructure:
    @pytest.fixture(scope="class")
    def html(self):
        p = Path(__file__).resolve().parent.parent
        return (p / "src/web/templates/_rpa_shared_funnel.html").read_text(encoding="utf-8")

    def test_kpi_alerts_section_present(self, html):
        assert "rpa-kpi-alerts" in html

    def test_kpi_alerts_badge_element(self, html):
        assert "rpa-kpi-alerts-badge" in html

    def test_ack_all_button_present(self, html):
        assert "rpa-kpi-alerts-ack-all" in html

    def test_refresh_alerts_function_exposed(self, html):
        assert "F._refreshAlerts" in html

    def test_render_alerts_function_exposed(self, html):
        assert "F._renderAlerts" in html

    def test_bind_ack_all_called_in_init(self, html):
        assert "F._bindAckAll()" in html

    def test_refresh_alerts_called_in_refresh(self, html):
        assert "F._refreshAlerts();" in html

    def test_ack_api_call_present(self, html):
        assert "/api/funnel/alerts/" in html

    def test_ack_all_api_call_present(self, html):
        assert "/api/funnel/alerts/ack-all" in html

    def test_fmt_ago_helper_present(self, html):
        assert "_fmtAgo" in html

    def test_css_classes_present(self, html):
        for cls in [
            ".rpa-kpi-alerts",
            ".rpa-kpi-alert-item",
            ".rpa-kpi-alert-item.warn",
            ".rpa-kpi-alert-item.critical",
            ".rpa-kpi-alert-item.acked",
            ".rpa-kpi-alerts-empty",
        ]:
            assert cls in html, f"CSS class missing: {cls}"


# ════════════════════════════════════════════════════════════════════════
# kpi_alerting module 结构测试
# ════════════════════════════════════════════════════════════════════════

class TestKpiAlertingModule:
    def test_default_thresholds_keys(self):
        for key in ("drop_pct_threshold", "abs_floor_pct", "min_days_required",
                    "min_daily_volume"):
            assert key in _DEFAULT_THRESHOLDS

    def test_kpi_defs_covers_four_rates(self):
        keys = {d[0] for d in _KPI_DEFS}
        assert keys == {"engaged_rate", "handoff_rate", "line_add_rate", "bonded_rate"}

    def test_kpi_defs_each_has_label_and_stage(self):
        for rate_key, label, vol_stage in _KPI_DEFS:
            assert rate_key
            assert label
            assert vol_stage

    def test_detect_kpi_drops_is_pure(self):
        """同一输入多次调用结果一致（纯函数）。"""
        series = _make_series(9, engaged_rate=60.0, today_engaged=10.0)
        r1 = detect_kpi_drops(series)
        r2 = detect_kpi_drops(series)
        assert r1 == r2
