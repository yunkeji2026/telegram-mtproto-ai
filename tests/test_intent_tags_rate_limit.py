"""P20-B / P21-A/B/C: per-IP rate limiter for intent-tags admin endpoints."""
from __future__ import annotations

import pytest


@pytest.fixture()
def _yaml_with_intent_tags(tmp_path, monkeypatch):
    """Point INTENT_TAGS_PATH at a writable test yaml."""
    yaml_file = tmp_path / "intent_tags.yaml"
    yaml_file.write_text("purchase:\n  - kw\n", encoding="utf-8")
    monkeypatch.setenv("INTENT_TAGS_PATH", str(yaml_file))
    from src.integrations import rpa_shared
    rpa_shared.reload_intent_tags()
    yield yaml_file
    monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
    rpa_shared.reload_intent_tags()


# ────────────────────────────────────────────────────────────────────────
# P20-B: /diff is rate-limited
# ────────────────────────────────────────────────────────────────────────


def test_diff_rate_limit_kicks_in(auth_client, _yaml_with_intent_tags) -> None:
    """Bombard /diff > capacity → 429."""
    body = {"content": "purchase:\n  - kw\n"}
    # Default capacity = 20 → 21st request must 429
    got_429 = False
    for _ in range(25):
        r = auth_client.post("/api/rpa/intent-tags/diff", json=body)
        if r.status_code == 429:
            got_429 = True
            assert "rate limit" in r.text.lower()
            break
        assert r.status_code == 200
    assert got_429, "no 429 within 25 rapid requests; bucket too large or no limit"


# ────────────────────────────────────────────────────────────────────────
# P21-A: trusted_proxies + X-Forwarded-For
# ────────────────────────────────────────────────────────────────────────


def test_xff_respected_when_direct_ip_is_trusted(auth_client, _yaml_with_intent_tags,
                                                   config_manager) -> None:
    """When request.client.host is in trusted_proxies, XFF first hop is used."""
    # TestClient sets request.client.host to 'testclient' — add it to trusted_proxies
    config_manager.config.setdefault("rpa", {})["trusted_proxies"] = ["testclient"]
    body = {"content": "purchase:\n  - kw\n"}

    # Two different XFF IPs → each has its own bucket
    # If XFF is respected, both can do up to capacity each
    r1 = auth_client.post("/api/rpa/intent-tags/diff", json=body,
                          headers={"X-Forwarded-For": "203.0.113.10"})
    assert r1.status_code == 200
    r2 = auth_client.post("/api/rpa/intent-tags/diff", json=body,
                          headers={"X-Forwarded-For": "203.0.113.20"})
    assert r2.status_code == 200


def test_xff_ignored_when_direct_ip_not_trusted(auth_client, _yaml_with_intent_tags,
                                                  config_manager) -> None:
    """trusted_proxies empty → XFF is ignored, all requests share testclient bucket."""
    config_manager.config.setdefault("rpa", {})["trusted_proxies"] = []
    body = {"content": "purchase:\n  - kw\n"}
    # Spam from "different" XFF should still hit same bucket
    got_429 = False
    for i in range(30):
        r = auth_client.post("/api/rpa/intent-tags/diff", json=body,
                             headers={"X-Forwarded-For": f"10.0.0.{i}"})
        if r.status_code == 429:
            got_429 = True
            break
    assert got_429, "XFF should be ignored without trusted_proxies; all requests share one bucket"


# ────────────────────────────────────────────────────────────────────────
# P21-B: /restore is also rate-limited (stricter)
# ────────────────────────────────────────────────────────────────────────


def test_restore_rate_limit_is_strict(auth_client, _yaml_with_intent_tags, app) -> None:
    """/restore (NOT dry_run) has capacity=5 → 6th rapid call must 429.

    P23-A: dry_run goes through diff bucket; only real restore hits the strict bucket.
    """
    if callable(getattr(app.state, "intent_tags_rate_limit_reset", None)):
        app.state.intent_tags_rate_limit_reset()
    body = {"filename": "intent_tags.yaml.bak_does_not_exist"}  # dry_run defaults to False
    saw_429 = False
    for _ in range(15):
        r = auth_client.post("/api/rpa/intent-tags/restore", json=body)
        if r.status_code == 429:
            saw_429 = True
            break
    assert saw_429


# ────────────────────────────────────────────────────────────────────────
# P21-C: rate-limit hits surface in /metrics
# ────────────────────────────────────────────────────────────────────────


# ────────────────────────────────────────────────────────────────────────
# P22-A: CIDR support in trusted_proxies
# ────────────────────────────────────────────────────────────────────────


def test_cidr_matches_xff_when_direct_in_range(auth_client, _yaml_with_intent_tags,
                                                  config_manager, app) -> None:
    """If trusted_proxies contains 'testclient' (TestClient literal) plus CIDR,
    XFF from spoofed clients still gets independent buckets."""
    config_manager.config.setdefault("rpa", {})["trusted_proxies"] = ["testclient", "10.0.0.0/8"]
    # Reset to clear any P21 test state
    if callable(getattr(app.state, "intent_tags_rate_limit_reset", None)):
        app.state.intent_tags_rate_limit_reset()
    body = {"content": "purchase:\n  - kw\n"}
    # All TestClient requests come from 'testclient' (literal-matched), so XFF is honored
    r = auth_client.post("/api/rpa/intent-tags/diff", json=body,
                          headers={"X-Forwarded-For": "203.0.113.99"})
    assert r.status_code == 200


def test_cidr_invalid_entry_skipped(auth_client, _yaml_with_intent_tags,
                                      config_manager, app) -> None:
    """Bogus entries don't crash; they degrade to non-trusted (XFF ignored)."""
    config_manager.config.setdefault("rpa", {})["trusted_proxies"] = [
        "not.a.valid.cidr",  # invalid -> treated as literal but won't match
        "10.0.0.0/8",        # valid CIDR but TestClient is 'testclient', not in 10.x
    ]
    if callable(getattr(app.state, "intent_tags_rate_limit_reset", None)):
        app.state.intent_tags_rate_limit_reset()
    body = {"content": "purchase:\n  - kw\n"}
    # With XFF ignored, all requests share testclient bucket; should hit limit fast
    got_429 = False
    for _ in range(30):
        r = auth_client.post("/api/rpa/intent-tags/diff", json=body,
                             headers={"X-Forwarded-For": "10.99.99.99"})
        if r.status_code == 429:
            got_429 = True
            break
    assert got_429


# ────────────────────────────────────────────────────────────────────────
# P22-B: rate-limit state reset across tests + idle sweep
# ────────────────────────────────────────────────────────────────────────


def test_rate_limit_reset_helper_clears_state(auth_client, _yaml_with_intent_tags,
                                                config_manager, app) -> None:
    """app.state.intent_tags_rate_limit_reset should empty buckets so 429 → 200 again."""
    config_manager.config.setdefault("rpa", {})["trusted_proxies"] = []
    body = {"content": "purchase:\n  - kw\n"}
    # Hit the limiter
    for _ in range(30):
        auth_client.post("/api/rpa/intent-tags/diff", json=body)
    r1 = auth_client.post("/api/rpa/intent-tags/diff", json=body)
    assert r1.status_code == 429

    # Reset and confirm next request passes
    reset = getattr(app.state, "intent_tags_rate_limit_reset", None)
    assert callable(reset)
    reset()
    r2 = auth_client.post("/api/rpa/intent-tags/diff", json=body)
    assert r2.status_code == 200


# ────────────────────────────────────────────────────────────────────────
# P23-A: dry_run goes through diff bucket (lenient) not restore bucket (strict)
# ────────────────────────────────────────────────────────────────────────


def test_dry_run_not_restricted_by_restore_bucket(auth_client, _yaml_with_intent_tags,
                                                    app, _yaml_with_intent_tags_creates_backup) -> None:
    """dry_run goes through diff bucket (cap=20), not restore bucket (cap=5)."""
    if callable(getattr(app.state, "intent_tags_rate_limit_reset", None)):
        app.state.intent_tags_rate_limit_reset()
    # 7 dry_run preview requests would exceed restore cap (5), but diff cap (20) allows them
    body = {"filename": _yaml_with_intent_tags_creates_backup, "dry_run": True}
    for i in range(7):
        r = auth_client.post("/api/rpa/intent-tags/restore", json=body)
        # Each may return 200 (diff returned) or 400 (if backup not found) — never 429 with cap=20
        assert r.status_code != 429, f"req #{i}: dry_run unexpectedly rate-limited"


@pytest.fixture()
def _yaml_with_intent_tags_creates_backup(_yaml_with_intent_tags) -> str:
    """Create a backup so dry_run has a real file to diff against."""
    from src.integrations import rpa_shared
    rpa_shared.write_intent_tags_yaml("purchase:\n  - changed\n")
    backups = rpa_shared.list_intent_tags_backups()
    assert backups
    return backups[0]["filename"]


# ────────────────────────────────────────────────────────────────────────
# P23-B: rate-limit hits are logged to audit_store
# ────────────────────────────────────────────────────────────────────────


def test_rate_limit_writes_audit_log(auth_client, _yaml_with_intent_tags, app) -> None:
    """When /diff returns 429, audit_store records 'rpa_intent_tags_rate_limited'."""
    if callable(getattr(app.state, "intent_tags_rate_limit_reset", None)):
        app.state.intent_tags_rate_limit_reset()
    body = {"content": "purchase:\n  - kw\n"}
    # Trigger >= 1 rate limit
    for _ in range(30):
        auth_client.post("/api/rpa/intent-tags/diff", json=body)
    # Query the same audit_store instance the route used (via app.state)
    audit_store = app.state.audit_store
    rows = audit_store.query(action="rpa_intent_tags_rate_limited", limit=100)
    assert rows, "audit log missing rate_limited entry"


# ────────────────────────────────────────────────────────────────────────
# P23-D: /metrics is rate-limited (lenient cap=100)
# ────────────────────────────────────────────────────────────────────────


def test_metrics_endpoint_rate_limited(auth_client, _yaml_with_intent_tags, app) -> None:
    """/metrics has its own bucket (cap=100, refill=1/s) — can be exceeded."""
    if callable(getattr(app.state, "intent_tags_rate_limit_reset", None)):
        app.state.intent_tags_rate_limit_reset()
    # Default cap=100 — burst 110 → some must 429
    got_429 = False
    for _ in range(110):
        r = auth_client.get("/api/rpa/metrics")
        if r.status_code == 429:
            got_429 = True
            break
        assert r.status_code == 200
    assert got_429, "no /metrics rate limit after 110 rapid requests"


def test_metrics_endpoint_label_in_counter(auth_client, _yaml_with_intent_tags, app) -> None:
    """/metrics output always exposes the 'metrics' endpoint counter label."""
    if callable(getattr(app.state, "intent_tags_rate_limit_reset", None)):
        app.state.intent_tags_rate_limit_reset()
    r = auth_client.get("/api/rpa/metrics")
    assert r.status_code == 200
    # Label must be present even when counter is 0 (no rate limit hits yet)
    assert 'rpa_intent_tags_rate_limited_total{endpoint="metrics"}' in r.text
    assert 'rpa_intent_tags_rate_limited_total{endpoint="diff"}' in r.text
    assert 'rpa_intent_tags_rate_limited_total{endpoint="restore"}' in r.text


# ────────────────────────────────────────────────────────────────────────
# P24-A: audit log throttling (same IP, same endpoint coalesced within 1s)
# ────────────────────────────────────────────────────────────────────────


def test_audit_log_is_throttled_under_burst(auth_client, _yaml_with_intent_tags, app) -> None:
    """30 rapid 429s from one IP → audit_log should have <= 2 entries (1s window)."""
    if callable(getattr(app.state, "intent_tags_rate_limit_reset", None)):
        app.state.intent_tags_rate_limit_reset()
    body = {"content": "purchase:\n  - kw\n"}
    for _ in range(40):
        auth_client.post("/api/rpa/intent-tags/diff", json=body)
    audit_store = app.state.audit_store
    rows = audit_store.query(action="rpa_intent_tags_rate_limited", limit=200)
    # Burst takes < 1s on TestClient → expect exactly 1 audit row (throttle window)
    # Allow 2 to tolerate slow CI / clock granularity
    assert 1 <= len(rows) <= 2, f"expected 1-2 throttled audit rows, got {len(rows)}"


# ────────────────────────────────────────────────────────────────────────
# P24-B: 429 responses include Retry-After header
# ────────────────────────────────────────────────────────────────────────


def test_429_has_retry_after_header(auth_client, _yaml_with_intent_tags, app) -> None:
    """When /diff returns 429, response must include Retry-After header."""
    if callable(getattr(app.state, "intent_tags_rate_limit_reset", None)):
        app.state.intent_tags_rate_limit_reset()
    body = {"content": "purchase:\n  - kw\n"}
    found_429 = None
    for _ in range(30):
        r = auth_client.post("/api/rpa/intent-tags/diff", json=body)
        if r.status_code == 429:
            found_429 = r
            break
    assert found_429 is not None
    retry_after = found_429.headers.get("retry-after")
    assert retry_after is not None, "Retry-After header missing"
    assert int(retry_after) >= 1
    # /diff refill=2/s → Retry-After should be at most ~1s
    assert int(retry_after) <= 5


def test_restore_429_retry_after_is_longer(auth_client, _yaml_with_intent_tags, app) -> None:
    """/restore is stricter (refill=0.2/s) → Retry-After should be larger than /diff."""
    if callable(getattr(app.state, "intent_tags_rate_limit_reset", None)):
        app.state.intent_tags_rate_limit_reset()
    body = {"filename": "intent_tags.yaml.bak_does_not_exist"}
    found_429 = None
    for _ in range(15):
        r = auth_client.post("/api/rpa/intent-tags/restore", json=body)
        if r.status_code == 429:
            found_429 = r
            break
    assert found_429 is not None
    retry_after = int(found_429.headers["retry-after"])
    # refill=0.2/s → need ~5s for one token
    assert retry_after >= 2, f"expected slow Retry-After for /restore, got {retry_after}"


# ────────────────────────────────────────────────────────────────────────
# P24-C: body size limits (413 Request Entity Too Large)
# ────────────────────────────────────────────────────────────────────────


def test_diff_rejects_oversize_body(auth_client, _yaml_with_intent_tags, app) -> None:
    """/diff body > 2MB → 413 Payload Too Large (not 400 / 429)."""
    if callable(getattr(app.state, "intent_tags_rate_limit_reset", None)):
        app.state.intent_tags_rate_limit_reset()
    # 3MB of bogus YAML (well over 2MB default)
    huge = "purchase:\n  - " + "x" * (3 * 1024 * 1024) + "\n"
    r = auth_client.post("/api/rpa/intent-tags/diff", json={"content": huge})
    assert r.status_code == 413, f"expected 413, got {r.status_code}"
    assert "too large" in r.text.lower()


def test_restore_rejects_oversize_body(auth_client, _yaml_with_intent_tags, app) -> None:
    """/restore body > 2MB → 413."""
    if callable(getattr(app.state, "intent_tags_rate_limit_reset", None)):
        app.state.intent_tags_rate_limit_reset()
    huge_filename = "x" * (3 * 1024 * 1024)
    r = auth_client.post("/api/rpa/intent-tags/restore", json={"filename": huge_filename})
    assert r.status_code == 413


def test_write_allows_larger_body_up_to_4mb(auth_client, _yaml_with_intent_tags, app) -> None:
    """/write has 4MB limit (vs default 2MB) — accepts 3MB but rejects 5MB."""
    if callable(getattr(app.state, "intent_tags_rate_limit_reset", None)):
        app.state.intent_tags_rate_limit_reset()
    # 5MB → over write limit
    too_huge = "purchase:\n  - " + "x" * (5 * 1024 * 1024) + "\n"
    r = auth_client.post("/api/rpa/intent-tags", json={"content": too_huge})
    assert r.status_code == 413


def test_content_length_header_pre_check(auth_client, _yaml_with_intent_tags, app) -> None:
    """If Content-Length declares > max, reject early without reading body."""
    if callable(getattr(app.state, "intent_tags_rate_limit_reset", None)):
        app.state.intent_tags_rate_limit_reset()
    # TestClient computes Content-Length from json= payload — over the 2MB limit
    huge = "purchase:\n  - " + "x" * (3 * 1024 * 1024) + "\n"
    r = auth_client.post("/api/rpa/intent-tags/diff", json={"content": huge})
    assert r.status_code == 413


def test_metrics_exposes_rate_limited_counter(auth_client, _yaml_with_intent_tags) -> None:
    """After triggering rate limit, /metrics shows the counter > 0."""
    body = {"content": "purchase:\n  - kw\n"}
    # Trigger at least 1 rate-limit hit
    for _ in range(30):
        auth_client.post("/api/rpa/intent-tags/diff", json=body)
    r = auth_client.get("/api/rpa/metrics")
    assert r.status_code == 200
    txt = r.text
    assert 'rpa_intent_tags_rate_limited_total{endpoint="diff"}' in txt


# ════════════════════════════════════════════════════════════════════════
# P25-A: middleware-level body size limit (covers ALL admin POST routes)
# P25-B: 413 writes audit log (attack signal)
# ════════════════════════════════════════════════════════════════════════


def test_middleware_blocks_oversize_on_non_intent_route(auth_client, app) -> None:
    """P25-A: even non-intent-tags POST routes are protected by middleware (2MB default)."""
    if callable(getattr(app.state, "intent_tags_rate_limit_reset", None)):
        app.state.intent_tags_rate_limit_reset()
    # Use ANY admin POST endpoint — try a generic write endpoint
    # If route doesn't exist 404 fires *before* body parsing in starlette, so we need
    # to pick a real POST route. /api/rpa/intent-tags/diff works.
    huge = "x" * (3 * 1024 * 1024)
    r = auth_client.post("/api/rpa/intent-tags/diff", json={"content": huge})
    assert r.status_code == 413
    # P25-A's middleware response uses "request body too large" wording
    assert "too large" in r.text.lower()


def test_413_writes_audit_log_with_oversize_action(auth_client, app) -> None:
    """P25-B: when middleware rejects 413, audit_log gains a web_body_oversize_rejected row."""
    audit_store = app.state.audit_store
    before = audit_store.query(action="web_body_oversize_rejected", limit=200)
    huge = "x" * (3 * 1024 * 1024)
    r = auth_client.post("/api/rpa/intent-tags/diff", json={"content": huge})
    assert r.status_code == 413
    after = audit_store.query(action="web_body_oversize_rejected", limit=200)
    assert len(after) > len(before), "413 should have written an audit row"
    # P25-B: row should include path + observed + limit
    row = after[0]
    target = row.get("target") or row.get("new_val") or ""
    # SQLite schema uses 'target' (action target col)
    serialized = " ".join(str(v) for v in row.values())
    assert "/api/rpa/intent-tags/diff" in serialized
    assert "limit=" in serialized


def test_413_audit_log_is_throttled_per_ip(auth_client, app) -> None:
    """P25-B: 10 oversize requests from same IP → audit_log gets <= 2 rows (5s throttle)."""
    audit_store = app.state.audit_store
    before = len(audit_store.query(action="web_body_oversize_rejected", limit=500))
    huge = "x" * (3 * 1024 * 1024)
    for _ in range(10):
        r = auth_client.post("/api/rpa/intent-tags/diff", json={"content": huge})
        assert r.status_code == 413
    after = len(audit_store.query(action="web_body_oversize_rejected", limit=500))
    delta = after - before
    # Burst takes < 5s on TestClient → expect 1 row (throttle), allow 2 for slow CI
    assert 1 <= delta <= 2, f"expected 1-2 throttled audit rows, got {delta}"


def test_middleware_allows_normal_size_post(auth_client, _yaml_with_intent_tags, app) -> None:
    """P25-A: legitimate small POSTs pass through middleware unchanged."""
    if callable(getattr(app.state, "intent_tags_rate_limit_reset", None)):
        app.state.intent_tags_rate_limit_reset()
    r = auth_client.post("/api/rpa/intent-tags/diff",
                         json={"content": "purchase:\n  - kw\n"})
    assert r.status_code == 200


def test_middleware_per_route_override_allows_larger_write(auth_client,
                                                             _yaml_with_intent_tags) -> None:
    """P25-A: /api/rpa/intent-tags (write) has 4MB override; 3MB body should succeed."""
    # 3MB YAML, valid syntax
    big_valid = "purchase:\n" + "  - kw\n" + ("  - k\n" * (3 * 1024 * 1024 // 6))
    r = auth_client.post("/api/rpa/intent-tags", json={"content": big_valid})
    # Either 200 (write succeeds) or 400 (validation rejects extra keywords) — but NOT 413
    assert r.status_code != 413, "write endpoint should allow up to 4MB"


# ════════════════════════════════════════════════════════════════════════
# P25-C: _audit_throttle uses OrderedDict + Lock (concurrent-safe)
# ════════════════════════════════════════════════════════════════════════


def test_audit_throttle_concurrent_burst_is_safe(auth_client, _yaml_with_intent_tags,
                                                   app) -> None:
    """P25-C: thread-pool burst of 429s → still <= 2 audit rows (lock prevents race dup)."""
    import concurrent.futures
    if callable(getattr(app.state, "intent_tags_rate_limit_reset", None)):
        app.state.intent_tags_rate_limit_reset()
    audit_store = app.state.audit_store
    before = len(audit_store.query(action="rpa_intent_tags_rate_limited", limit=500))

    body = {"content": "purchase:\n  - kw\n"}

    def _spam():
        return auth_client.post("/api/rpa/intent-tags/diff", json=body).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        codes = list(ex.map(lambda _: _spam(), range(50)))
    assert 429 in codes, "concurrent burst should trigger rate limit"

    after = len(audit_store.query(action="rpa_intent_tags_rate_limited", limit=500))
    delta = after - before
    # Even with 8 threads racing, lock + throttle → <= 2 rows
    assert 1 <= delta <= 2, f"concurrent audit rows leaked: got {delta}"


# ════════════════════════════════════════════════════════════════════════
# P25-D: P19/P20 audit hooks revival smoke-test (verifies P23-B fix
#         exposed audit_store correctly so write/restore/reload audit
#         rows are actually written, not silently dropped).
# ════════════════════════════════════════════════════════════════════════


def test_write_audit_hook_fires(auth_client, _yaml_with_intent_tags, app) -> None:
    """P25-D: POST /api/rpa/intent-tags → audit_log gets rpa_intent_tags_write row."""
    audit_store = app.state.audit_store
    before = len(audit_store.query(action="rpa_intent_tags_write", limit=200))
    r = auth_client.post("/api/rpa/intent-tags",
                         json={"content": "purchase:\n  - newkw\n"})
    assert r.status_code == 200, r.text
    after = len(audit_store.query(action="rpa_intent_tags_write", limit=200))
    assert after == before + 1, "rpa_intent_tags_write audit hook should fire"


def test_reload_audit_hook_fires(auth_client, _yaml_with_intent_tags, app) -> None:
    """P25-D: POST /api/rpa/intent-tags/reload → audit_log gets rpa_intent_tags_reload row."""
    audit_store = app.state.audit_store
    before = len(audit_store.query(action="rpa_intent_tags_reload", limit=200))
    r = auth_client.post("/api/rpa/intent-tags/reload")
    assert r.status_code == 200, r.text
    after = len(audit_store.query(action="rpa_intent_tags_reload", limit=200))
    assert after == before + 1, "rpa_intent_tags_reload audit hook should fire"


# ════════════════════════════════════════════════════════════════════════
# P26-B: web_body_oversize_rejected_total Prometheus counter
# ════════════════════════════════════════════════════════════════════════


def test_metrics_exposes_oversize_counter_when_no_attack(auth_client, app) -> None:
    """P26-B: even with no 413, the metric line is emitted (default 0)."""
    if callable(getattr(app.state, "intent_tags_rate_limit_reset", None)):
        app.state.intent_tags_rate_limit_reset()
    # reset oversize counter
    app.state.web_body_oversize_counter = {}
    r = auth_client.get("/api/rpa/metrics")
    assert r.status_code == 200
    assert "web_body_oversize_rejected_total" in r.text


def test_metrics_oversize_counter_increments_per_path(auth_client, app) -> None:
    """P26-B: each 413 increments the per-path counter (no throttle on counter)."""
    app.state.web_body_oversize_counter = {}
    huge = "x" * (3 * 1024 * 1024)
    for _ in range(3):
        r = auth_client.post("/api/rpa/intent-tags/diff", json={"content": huge})
        assert r.status_code == 413
    r = auth_client.get("/api/rpa/metrics")
    assert r.status_code == 200
    # All 3 attacks counted (counter is NOT audit-throttled)
    assert 'web_body_oversize_rejected_total{path="/api/rpa/intent-tags/diff"} 3' in r.text


def test_metrics_exposes_watcher_running_gauge(auth_client) -> None:
    """P26-A/B: watcher status gauge is emitted (0 or 1)."""
    r = auth_client.get("/api/rpa/metrics")
    assert r.status_code == 200
    assert "rpa_intent_tags_watcher_running" in r.text
    assert "rpa_intent_tags_auto_reloads_total" in r.text


# ════════════════════════════════════════════════════════════════════════
# P26-C: body_limits dict from config.yaml::web_admin
# ════════════════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════════════════
# P27-B: X-Body-Limit header on 413 (client-friendly cap discovery)
# ════════════════════════════════════════════════════════════════════════


def test_413_response_includes_x_body_limit_header(auth_client, app) -> None:
    """P27-B: every 413 carries X-Body-Limit telling client the max."""
    app.state.web_body_oversize_counter = {}
    huge = "x" * (3 * 1024 * 1024)
    r = auth_client.post("/api/rpa/intent-tags/diff", json={"content": huge})
    assert r.status_code == 413
    cap = r.headers.get("x-body-limit")
    assert cap is not None, "X-Body-Limit header missing"
    assert int(cap) == 2 * 1024 * 1024, f"diff endpoint should report 2MB cap, got {cap}"


def test_413_response_body_includes_max_body_bytes(auth_client, app) -> None:
    """P27-B: JSON body contains max_body_bytes for non-header-aware clients."""
    app.state.web_body_oversize_counter = {}
    huge = "x" * (3 * 1024 * 1024)
    r = auth_client.post("/api/rpa/intent-tags/diff", json={"content": huge})
    assert r.status_code == 413
    body = r.json()
    assert body.get("max_body_bytes") == 2 * 1024 * 1024


def test_413_x_body_limit_reflects_per_path_override(auth_client, app) -> None:
    """P27-B: /api/rpa/intent-tags (write) has 4MB cap → X-Body-Limit shows 4MB."""
    app.state.web_body_oversize_counter = {}
    # 5MB to overshoot the 4MB write limit
    too_huge = "x" * (5 * 1024 * 1024)
    r = auth_client.post("/api/rpa/intent-tags", json={"content": too_huge})
    assert r.status_code == 413
    cap = r.headers.get("x-body-limit")
    assert int(cap) == 4 * 1024 * 1024, f"write endpoint should report 4MB cap, got {cap}"


def test_metrics_body_limits_dict_loaded(auth_client, app, config_manager) -> None:
    """P26-C: body_limits in config should be applied at app build time.

    Smoke test: write endpoint still uses 4MB (default override path), and
    other endpoints still use 2MB default. Hard to introspect closure state
    so we check behavior:
      - /api/rpa/intent-tags accepts a 3MB body (would be 413 at 2MB)
      - /api/rpa/intent-tags/diff rejects 3MB body
    """
    # 3MB body — fits write (4MB limit), exceeds diff (2MB limit)
    huge_yaml = "purchase:\n" + ("  - " + "x" * 100 + "\n") * 30000  # ~3MB
    assert len(huge_yaml) > 2 * 1024 * 1024
    assert len(huge_yaml) < 4 * 1024 * 1024

    r_write = auth_client.post("/api/rpa/intent-tags", json={"content": huge_yaml})
    # 200 or 400 (validation), NOT 413 — write endpoint has 4MB cap from body_limits
    assert r_write.status_code != 413, \
        f"write endpoint should accept 3MB body (config override); got {r_write.status_code}"

    r_diff = auth_client.post("/api/rpa/intent-tags/diff", json={"content": huge_yaml})
    assert r_diff.status_code == 413, "diff endpoint should still reject 3MB (default 2MB)"
