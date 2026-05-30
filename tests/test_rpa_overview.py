"""P4 RPA 跨平台总览 —— 单元 + 路由烟雾测试。

覆盖：
- `_summarize_via_service` 对 LINE / WhatsApp / Messenger status dict 的
  字段兼容映射；对 `None` 与抛异常 service 的稳健返回。
- `_summarize_telegram` 从 `app.state.telegram_client` 读 running，
  缺失 client 时 unavailable=true 的早退路径。
- `_collect_pending` / `_collect_alerts` 跨平台合并 + 倒序 + Messenger 用
  `list_approvals` 而非 `list_pending` 的"鸭子类型"分支。
- 3 个 HTTP 端点（/status, /pending, /alerts）走 TestClient + noop auth：
  字段结构 / 聚合计数 / limit 参数边界（clamp 到 [1,50]）。

本套件不依赖任何真实 service / state store：所有 svc 都是手写的
``SimpleNamespace`` / 类 stub，避免 sqlite / ADB 副作用。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from starlette.testclient import TestClient

from src.integrations.rpa_base import RpaPlatform, RpaStatusSummary
from src.web.routes import rpa_overview_routes as ov


# ════════════════════════════════════════════════════════════════════════
# Stubs
# ════════════════════════════════════════════════════════════════════════


class _StubLineService:
    """模拟 LINE service.status() 返回值结构。"""

    def __init__(
        self,
        *,
        running: bool = True,
        enabled: bool = True,
        sent: int = 12,
        total: int = 14,
        pending_stats: Optional[Dict[str, int]] = None,
        alerts_unacked: int = 2,
        reply_mode: str = "approve",
        pending_rows: Optional[List[Dict[str, Any]]] = None,
        alert_rows: Optional[List[Dict[str, Any]]] = None,
    ):
        self._running = running
        self._enabled = enabled
        self._sent = sent
        self._total = total
        self._pending_stats = pending_stats or {"pending": 3}
        self._alerts_unacked = alerts_unacked
        self._reply_mode = reply_mode
        self._pending_rows = pending_rows or []
        self._alert_rows = alert_rows or []

    def status(self) -> Dict[str, Any]:
        return {
            "enabled_cfg": self._enabled,
            "running": self._running,
            "paused": False,
            "pause_remaining_sec": 0,
            "stats_24h": {
                "sent": self._sent,
                "total": self._total,
                "avg_send_ms": 850.0,
            },
            "pending_stats": self._pending_stats,
            "alerts_unacked": self._alerts_unacked,
            "reply_mode": self._reply_mode,
            "daily_cap": 200,
            "daily_sent": self._sent,
            "last_tick_ts": 1_700_000_000,
        }

    def list_pending(self, *, status=None, limit=50):
        return list(self._pending_rows[:limit])

    def list_alerts(self, *, only_unacked=True, limit=50):
        return list(self._alert_rows[:limit])


class _StubWhatsAppService:
    """WhatsApp status 字段命名稍有差异（直接 enabled + pending_count）。"""

    def __init__(
        self,
        *,
        running: bool = True,
        enabled: bool = True,
        sent: int = 5,
        total: int = 5,
        pending_count: int = 1,
        alerts_unacked: int = 0,
        pending_rows: Optional[List[Dict[str, Any]]] = None,
        alert_rows: Optional[List[Dict[str, Any]]] = None,
    ):
        self._st = {
            "enabled": enabled,
            "running": running,
            "paused": False,
            "pause_remaining_sec": 0,
            "stats_24h": {"sent": sent, "total": total, "avg_ms": 600.0},
            "stats_1h": {},
            "unacked_alerts": alerts_unacked,
            "pending_count": pending_count,
            "daily_cap": 100,
            "daily_sent": sent,
            "reply_mode": "auto",
        }
        self._pending_rows = pending_rows or []
        self._alert_rows = alert_rows or []

    def status(self) -> Dict[str, Any]:
        return dict(self._st)

    def list_pending(self, *, status=None, limit=50):
        return list(self._pending_rows[:limit])

    def list_alerts(self, *, only_unacked=True, limit=50):
        return list(self._alert_rows[:limit])


class _StubMessengerService:
    """Messenger 没有顶层 `enabled` / `stats_24h` / `pending_count`。

    走 `send_counters` + `approval_sla.pending_count`。本 stub 模拟这一形态，
    用来验证 `_summarize_via_service` 的 Messenger 分支映射。
    """

    def __init__(
        self,
        *,
        running: bool = True,
        sent_24h: int = 8,
        total_24h: int = 10,
        pending_count: int = 2,
        approval_rows: Optional[List[Dict[str, Any]]] = None,
    ):
        self._st = {
            "running": running,
            "send_counters": {
                "sent_24h": sent_24h,
                "total_24h": total_24h,
                "avg_ms_24h": 720.0,
            },
            "approval_sla": {"pending_count": pending_count},
            "last_tick_ts": 1_700_000_500,
        }
        self._approval_rows = approval_rows or []

    def status(self) -> Dict[str, Any]:
        return dict(self._st)

    def list_approvals(self, *, status=None, limit=50):
        # Messenger 故意不暴露 list_pending，验证鸭子类型分支
        return list(self._approval_rows[:limit])


class _BrokenService:
    """status() 抛异常的 service，用来验证错误兜底。"""

    def status(self):
        raise RuntimeError("simulated svc failure")


# ════════════════════════════════════════════════════════════════════════
# _summarize_via_service
# ════════════════════════════════════════════════════════════════════════


def test_summarize_none_service_returns_unavailable():
    s = ov._summarize_via_service(RpaPlatform.LINE, None)
    assert isinstance(s, RpaStatusSummary)
    assert s.platform is RpaPlatform.LINE
    assert s.available is False
    assert s.enabled is False
    assert "未启用" in s.hint or "未构建" in s.hint


def test_summarize_line_basic_mapping():
    svc = _StubLineService(running=True, enabled=True, sent=12, total=14)
    s = ov._summarize_via_service(RpaPlatform.LINE, svc)
    assert s.platform is RpaPlatform.LINE
    assert s.available is True
    assert s.enabled is True
    assert s.running is True
    assert s.sent_24h == 12
    assert s.total_24h == 14
    assert s.pending_count == 3  # 来自 pending_stats.pending
    assert s.unacked_alerts == 2
    assert s.reply_mode == "approve"
    assert s.daily_cap == 200
    # success_rate = 12/14*100 = 85.7
    assert 85.0 <= s.success_rate <= 86.0
    assert s.health_status == "warn"  # 有 unacked_alerts → warn


def test_summarize_whatsapp_basic_mapping():
    svc = _StubWhatsAppService(running=True, sent=5, total=5, pending_count=1)
    s = ov._summarize_via_service(RpaPlatform.WHATSAPP, svc)
    assert s.available is True
    assert s.enabled is True
    assert s.running is True
    assert s.sent_24h == 5
    assert s.pending_count == 1
    assert s.unacked_alerts == 0
    assert s.success_rate == 100.0
    assert s.health_status == "ok"


def test_summarize_messenger_send_counters_to_stats_24h():
    """Messenger 没有 stats_24h，要从 send_counters 转换。"""
    svc = _StubMessengerService(sent_24h=8, total_24h=10, pending_count=2)
    s = ov._summarize_via_service(RpaPlatform.MESSENGER, svc)
    assert s.available is True
    assert s.enabled is True  # 自动补齐
    assert s.running is True
    assert s.sent_24h == 8
    assert s.total_24h == 10
    assert s.pending_count == 2  # 从 approval_sla.pending_count 拿
    assert s.avg_ms_24h == 720.0


def test_summarize_messenger_stopped_marks_err():
    svc = _StubMessengerService(running=False)
    s = ov._summarize_via_service(RpaPlatform.MESSENGER, svc)
    assert s.available is True
    assert s.running is False
    # health: enabled=true but not running → err
    assert s.health_status == "err"


def test_summarize_handles_status_exception():
    s = ov._summarize_via_service(RpaPlatform.LINE, _BrokenService())
    assert s.available is False
    assert s.enabled is False
    assert "失败" in s.hint


# ════════════════════════════════════════════════════════════════════════
# _summarize_telegram
# ════════════════════════════════════════════════════════════════════════


def _make_request_with_state(**state_kwargs):
    """构造一个最小 Request 替身（只用 app.state.X 即可）。"""
    app = SimpleNamespace(state=SimpleNamespace(**state_kwargs))
    return SimpleNamespace(app=app)


def test_summarize_telegram_no_client():
    req = _make_request_with_state()  # 没注入 telegram_client
    s = ov._summarize_telegram(req)
    assert s.platform is RpaPlatform.TELEGRAM
    assert s.available is False
    assert s.enabled is False


def test_summarize_telegram_with_running_client():
    client = SimpleNamespace(running=True, _last_send_wallclock=1_700_000_900)
    req = _make_request_with_state(telegram_client=client, config_manager=None)
    s = ov._summarize_telegram(req)
    assert s.available is True
    assert s.running is True
    assert s.enabled is True  # 默认主入口
    assert s.last_run_ts == 1_700_000_900.0
    assert s.reply_mode == "auto"  # MTProto = auto


def test_summarize_telegram_offline_client():
    client = SimpleNamespace(running=False, _last_send_wallclock=0)
    req = _make_request_with_state(telegram_client=client, config_manager=None)
    s = ov._summarize_telegram(req)
    assert s.available is True
    assert s.running is False
    # enabled=true but not running → err
    assert s.health_status == "err"


# ════════════════════════════════════════════════════════════════════════
# _collect_summaries
# ════════════════════════════════════════════════════════════════════════


def test_collect_summaries_returns_all_four():
    client = SimpleNamespace(running=True, _last_send_wallclock=0)
    req = _make_request_with_state(
        telegram_client=client,
        line_rpa_service=_StubLineService(),
        messenger_rpa_service=_StubMessengerService(),
        whatsapp_rpa_service=_StubWhatsAppService(),
        config_manager=None,
    )
    out = ov._collect_summaries(req)
    assert len(out) == 4
    platforms = [s.platform for s in out]
    assert platforms == [
        RpaPlatform.TELEGRAM,
        RpaPlatform.LINE,
        RpaPlatform.MESSENGER,
        RpaPlatform.WHATSAPP,
    ]


def test_collect_summaries_handles_missing_services():
    req = _make_request_with_state()  # 所有都缺
    out = ov._collect_summaries(req)
    assert len(out) == 4
    assert all(not s.available for s in out)


# ════════════════════════════════════════════════════════════════════════
# _collect_pending
# ════════════════════════════════════════════════════════════════════════


def test_collect_pending_merges_and_sorts_desc():
    line_rows = [
        {"id": 1, "status": "pending", "ts": 1000.0,
         "chat_key": "L1", "peer_text": "hi from line",
         "proposed_reply": "ok"},
    ]
    wa_rows = [
        {"id": 2, "status": "pending", "ts": 3000.0,
         "chat_key": "W1", "peer_text": "hi wa",
         "proposed_reply": "sure"},
    ]
    msgr_rows = [
        # Messenger 用 reply_text 而非 proposed_reply
        {"id": 3, "status": "pending", "created_at": 2000.0,
         "chat_key": "M1", "peer_text": "hi msgr", "reply_text": "ack"},
    ]
    req = _make_request_with_state(
        line_rpa_service=_StubLineService(pending_rows=line_rows),
        messenger_rpa_service=_StubMessengerService(approval_rows=msgr_rows),
        whatsapp_rpa_service=_StubWhatsAppService(pending_rows=wa_rows),
    )
    out = ov._collect_pending(req, limit_per_platform=5)
    assert len(out) == 3
    # ts 倒序：WhatsApp(3000) > Messenger(2000) > LINE(1000)
    assert [r["platform"] for r in out] == [
        "whatsapp", "messenger", "line",
    ]
    # 字段统一：proposed_reply 必须填充（Messenger reply_text 也映射进来）
    msgr = next(r for r in out if r["platform"] == "messenger")
    assert msgr["proposed_reply"] == "ack"
    assert msgr["platform_name"] == "Messenger"


def test_collect_pending_respects_limit_per_platform():
    rows = [
        {"id": i, "status": "pending", "ts": float(i), "chat_key": f"K{i}",
         "peer_text": "x", "proposed_reply": "y"} for i in range(10)
    ]
    req = _make_request_with_state(
        line_rpa_service=_StubLineService(pending_rows=rows),
    )
    out = ov._collect_pending(req, limit_per_platform=3)
    assert len(out) == 3


def test_collect_pending_ignores_missing_services():
    req = _make_request_with_state()  # 没注入任何 svc
    assert ov._collect_pending(req) == []


# ════════════════════════════════════════════════════════════════════════
# _collect_alerts
# ════════════════════════════════════════════════════════════════════════


def test_collect_alerts_merges_line_and_whatsapp():
    line_alerts = [
        {"id": 1, "severity": "warning", "ts": 1500.0,
         "code": "ime_lost", "title": "输入法切换", "acked": False},
    ]
    wa_alerts = [
        {"id": 7, "severity": "error", "ts": 2500.0,
         "code": "send_fail", "title": "发送失败", "acked": False},
    ]
    req = _make_request_with_state(
        line_rpa_service=_StubLineService(alert_rows=line_alerts),
        whatsapp_rpa_service=_StubWhatsAppService(alert_rows=wa_alerts),
        messenger_rpa_service=_StubMessengerService(),  # 没有 list_alerts → 跳过
    )
    out = ov._collect_alerts(req, limit_per_platform=5)
    assert len(out) == 2
    # ts 倒序：WhatsApp(2500) > LINE(1500)
    assert out[0]["platform"] == "whatsapp"
    assert out[0]["severity"] == "error"
    assert out[1]["platform"] == "line"
    assert out[1]["code"] == "ime_lost"


def test_collect_alerts_skips_service_without_list_alerts():
    # Messenger stub 没有 list_alerts —— 不应抛错，应直接跳过
    req = _make_request_with_state(
        messenger_rpa_service=_StubMessengerService(),
    )
    assert ov._collect_alerts(req) == []


# ════════════════════════════════════════════════════════════════════════
# 路由烟雾测试（HTTP）
# ════════════════════════════════════════════════════════════════════════


def _noop_auth(request):
    return None


@pytest.fixture
def overview_app() -> FastAPI:
    """全 4 平台 svc + telegram client 全注入的 app。"""
    app = FastAPI()
    ov.register_rpa_overview_routes(
        app,
        page_auth=_noop_auth,
        api_auth=_noop_auth,
        templates=None,
    )
    app.state.telegram_client = SimpleNamespace(
        running=True, _last_send_wallclock=1_700_000_900
    )
    app.state.line_rpa_service = _StubLineService(
        pending_rows=[
            {"id": 1, "status": "pending", "ts": 1000.0,
             "chat_key": "L1", "peer_text": "x", "proposed_reply": "y"},
        ],
        alert_rows=[
            {"id": 11, "severity": "warning", "ts": 1500.0,
             "code": "ime_lost", "title": "T"},
        ],
    )
    app.state.messenger_rpa_service = _StubMessengerService(
        approval_rows=[
            {"id": 2, "status": "pending", "created_at": 2000.0,
             "chat_key": "M1", "peer_text": "x", "reply_text": "y"},
        ],
    )
    app.state.whatsapp_rpa_service = _StubWhatsAppService(
        pending_rows=[
            {"id": 3, "status": "pending", "ts": 3000.0,
             "chat_key": "W1", "peer_text": "x", "proposed_reply": "y"},
        ],
        alert_rows=[
            {"id": 22, "severity": "error", "ts": 2500.0,
             "code": "send_fail", "title": "T"},
        ],
    )
    return app


@pytest.fixture
def overview_client(overview_app: FastAPI) -> TestClient:
    return TestClient(overview_app)


def test_route_status_returns_aggregated_payload(overview_client: TestClient):
    r = overview_client.get("/api/rpa-overview/status")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "ts" in body
    assert isinstance(body["platforms"], list) and len(body["platforms"]) == 4
    # 平台顺序固定
    assert [p["platform"] for p in body["platforms"]] == [
        "telegram", "line", "messenger", "whatsapp",
    ]
    # 聚合字段
    agg = body["aggregate"]
    assert agg["platforms_total"] == 4
    assert agg["platforms_running"] == 4  # 全部 running
    assert agg["platforms_offline"] == 0
    # sent_24h 汇总：telegram(0) + line(12) + messenger(8) + whatsapp(5) = 25
    assert agg["sent_24h"] == 25
    # pending_total：line(3) + messenger(2) + whatsapp(1) = 6
    assert agg["pending_total"] == 6


def test_route_status_offline_when_services_missing():
    app = FastAPI()
    ov.register_rpa_overview_routes(
        app, page_auth=_noop_auth, api_auth=_noop_auth, templates=None,
    )
    # 不注入任何 service / client
    c = TestClient(app)
    r = c.get("/api/rpa-overview/status")
    assert r.status_code == 200
    body = r.json()
    assert body["aggregate"]["platforms_offline"] == 4
    assert body["aggregate"]["platforms_running"] == 0


def test_route_pending_returns_merged_sorted(overview_client: TestClient):
    r = overview_client.get("/api/rpa-overview/pending?limit=5")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    items = body["items"]
    assert len(items) == 3
    # 倒序：WhatsApp(3000) > Messenger(2000) > LINE(1000)
    assert [it["platform"] for it in items] == [
        "whatsapp", "messenger", "line",
    ]


def test_route_pending_clamps_limit(overview_client: TestClient):
    # limit 越界仍 200，参数被 clamp
    r1 = overview_client.get("/api/rpa-overview/pending?limit=0")
    r2 = overview_client.get("/api/rpa-overview/pending?limit=9999")
    assert r1.status_code == 200 and r2.status_code == 200


def test_route_alerts_returns_merged(overview_client: TestClient):
    r = overview_client.get("/api/rpa-overview/alerts?limit=10")
    assert r.status_code == 200
    body = r.json()
    items = body["items"]
    assert len(items) == 2
    # ts 倒序：WhatsApp(2500) > LINE(1500)
    assert items[0]["platform"] == "whatsapp"
    assert items[0]["severity"] == "error"
    assert items[1]["platform"] == "line"


def test_route_status_handles_broken_service():
    """单个 service 异常不应让聚合 API 500。"""
    app = FastAPI()
    ov.register_rpa_overview_routes(
        app, page_auth=_noop_auth, api_auth=_noop_auth, templates=None,
    )
    app.state.line_rpa_service = _BrokenService()
    app.state.messenger_rpa_service = _StubMessengerService()
    app.state.whatsapp_rpa_service = _StubWhatsAppService()
    c = TestClient(app)
    r = c.get("/api/rpa-overview/status")
    assert r.status_code == 200
    body = r.json()
    line = next(p for p in body["platforms"] if p["platform"] == "line")
    assert line["available"] is False
    # 其它平台依然正常
    wa = next(p for p in body["platforms"] if p["platform"] == "whatsapp")
    assert wa["available"] is True
