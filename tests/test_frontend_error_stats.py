"""前端「哑按钮」运行时错误观测门禁。

覆盖：
- FrontendErrorStats 记录/消毒/上限/dump/dump_prom；
- POST /api/telemetry/frontend-error → GET /api/workspace/metrics.frontend_errors 端到端。
"""
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.web.frontend_error_stats import FrontendErrorStats
from src.web.routes.drafts_routes import register_metrics_route, register_telemetry_route


# ── 单元：计数 / 消毒 / 上限 ───────────────────────────────────────────

def test_record_and_dump():
    s = FrontendErrorStats()
    s.record(page="/unified-inbox", fn="setMode", etype="ReferenceError")
    s.record(page="/unified-inbox", fn="setMode", etype="ReferenceError")
    s.record(page="/whatsapp-rpa", fn="waLoadPending", etype="ReferenceError")
    d = s.dump()
    assert d["total"] == 3
    assert d["by_fn"]["setMode"] == 2
    assert d["by_fn"]["waLoadPending"] == 1
    assert d["by_page"]["/unified-inbox"] == 2
    assert d["by_type"]["ReferenceError"] == 3
    assert d["overflow"] == 0
    # by_fn 按次数降序
    assert list(d["by_fn"].keys())[0] == "setMode"


def test_sanitization():
    s = FrontendErrorStats()
    # 查询串/hash 剥离
    s.record(page="/telegram?tab=x#frag", fn="foo", etype="ReferenceError")
    # 非法标识符 → unknown；未知类型 → Error
    s.record(page="/x", fn="not a fn()", etype="WeirdError")
    d = s.dump()
    assert "/telegram" in d["by_page"]
    assert all("?" not in p and "#" not in p for p in d["by_page"])
    assert d["by_fn"].get("unknown") == 1
    assert d["by_type"].get("Error") == 1


def test_empty_defaults_to_unknown():
    s = FrontendErrorStats()
    s.record()
    d = s.dump()
    assert d["by_page"].get("unknown") == 1
    assert d["by_fn"].get("unknown") == 1
    assert d["by_type"].get("Error") == 1


def test_distinct_key_cap_overflows():
    s = FrontendErrorStats()
    # 灌 150 个不同 fn（上限 100）→ 超出的归 __other__ 并计 overflow
    for i in range(150):
        s.record(page="/p", fn=f"fn{i}", etype="ReferenceError")
    d = s.dump()
    assert d["total"] == 150
    assert len(d["by_fn"]) <= 101  # 100 distinct + __other__
    assert "__other__" in d["by_fn"]
    assert d["overflow"] >= 49


def test_dump_prom_shape():
    s = FrontendErrorStats()
    s.record(page="/unified-inbox", fn="setMode", etype="ReferenceError")
    txt = s.dump_prom()
    assert "frontend_errors_total 1" in txt
    assert 'frontend_errors_by_fn_total{fn="setMode"} 1' in txt
    assert 'frontend_errors_by_page_total{page="/unified-inbox"} 1' in txt
    assert 'frontend_errors_by_type_total{type="ReferenceError"} 1' in txt


def test_prom_label_escaping():
    s = FrontendErrorStats()
    # page 消毒会去掉引号/反斜杠，但 _esc 仍应保证输出合法
    s.record(page='/a"b\\c', fn="x", etype="ReferenceError")
    txt = s.dump_prom()
    # 不应出现裸的未转义引号破坏 label
    assert "frontend_errors_by_page_total" in txt


# ── 端到端：beacon 写入 → metrics 读出 ───────────────────────────────

def _make_app(role="admin"):
    app = FastAPI()

    @app.middleware("http")
    async def _inject(req: Request, call_next):
        req.scope["session"] = {"role": role, "user_id": "u1"}
        return await call_next(req)

    def api_auth(r: Request):
        return True

    register_telemetry_route(app, api_auth=api_auth)
    register_metrics_route(app, api_auth=api_auth)
    return TestClient(app, raise_server_exceptions=True)


def test_beacon_then_metrics_roundtrip():
    from src.web.frontend_error_stats import get_frontend_error_stats
    get_frontend_error_stats().reset()
    c = _make_app(role="admin")

    r = c.post("/api/telemetry/frontend-error",
               json={"page": "/line-rpa", "fn": "lrRefresh", "type": "ReferenceError"})
    assert r.status_code == 200 and r.json().get("ok") is True

    m = c.get("/api/workspace/metrics").json()
    fe = m.get("frontend_errors")
    assert fe is not None
    assert fe["total"] >= 1
    assert fe["by_fn"].get("lrRefresh") == 1
    assert fe["by_page"].get("/line-rpa") == 1


def test_malformed_beacon_is_ok():
    """坏 body 不得 500——telemetry 绝不因脏输入影响前端。"""
    c = _make_app(role="admin")
    r = c.post("/api/telemetry/frontend-error", data="not json",
               headers={"content-type": "text/plain"})
    assert r.status_code == 200


def test_metrics_prometheus_includes_frontend_errors():
    from src.web.frontend_error_stats import get_frontend_error_stats
    get_frontend_error_stats().reset()
    c = _make_app(role="admin")
    c.post("/api/telemetry/frontend-error",
           json={"page": "/messenger-rpa", "fn": "saveConfig", "type": "ReferenceError"})
    r = c.get("/api/workspace/metrics?format=prometheus")
    assert r.status_code == 200
    assert "frontend_errors_total" in r.text
    assert 'fn="saveConfig"' in r.text
