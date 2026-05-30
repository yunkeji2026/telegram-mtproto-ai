"""W3-3D.4：relations 域性能基准测试。

默认 CI 跳过（pytest.ini ``-m "not benchmark"``）。手工触发：
    python -m pytest tests/test_perf_relations.py -m benchmark -q

SLO（写在断言里，回归 PR 必须保持）：
  - 1000 contacts × 30 天 trend API < 2.0s（含 cache miss 冷路径）
  - 同 key 第二次请求 < 50ms（cache 热路径）
  - intimacy-history 单 journey 60 天 < 200ms
"""
from __future__ import annotations

import time
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.contacts.gateway import ContactGateway
from src.contacts.handoff import HandoffTokenService
from src.contacts.merge import MergeService
from src.contacts.models import CHANNEL_MESSENGER
from src.contacts.store import ContactStore
from src.skills.intimacy_engine import IntimacyEngine
from src.web.routes.contacts_routes import (
    register_contacts_routes,
    _intimacy_trend_cache_clear,
)


pytestmark = pytest.mark.benchmark


@pytest.fixture
def big_client(tmp_path):
    """1000 contacts × 平均 30 events per journey = 30k events."""
    _intimacy_trend_cache_clear()
    store = ContactStore(db_path=tmp_path / "contacts.db")
    handoff = HandoffTokenService(store, ttl_seconds=3600)
    merge = MergeService(store)
    gateway = ContactGateway(store, handoff, merge)
    intim = IntimacyEngine(store)

    # 直接批量写库，绕过 gateway 的 ENGAGED 转推 + intimacy refresh 副作用
    now = int(time.time())
    contacts_data = []
    journeys_data = []
    cis_data = []
    events_data = []
    for i in range(1000):
        cid = uuid.uuid4().hex
        jid = uuid.uuid4().hex
        ciid = uuid.uuid4().hex
        contacts_data.append((cid, f"User{i}", "", "", "", now - i * 60, now, ""))
        journeys_data.append(
            (jid, cid, "default", "ENGAGED", 0.0, 0.0, 0.0, 0, 0, now, now, "{}"),
        )
        cis_data.append(
            (ciid, cid, CHANNEL_MESSENGER, "a", f"fb_{i}", "first_seen",
             now - i * 60, "", 0.0, f"User{i}"),
        )
        # 每 journey 30 events，分布在过去 30 天
        for d in range(30):
            ts = now - d * 86400 - 100  # 每天 1 条 msg_in
            events_data.append(
                (uuid.uuid4().hex, jid, "", "msg_in", "{}", ts),
            )
            ts2 = ts + 60
            events_data.append(
                (uuid.uuid4().hex, jid, "", "msg_out", "{}", ts2),
            )

    with store._lock:
        store._conn.executemany(
            "INSERT INTO contacts(contact_id, primary_name, language_hint, "
            "timezone_hint, country_hint, created_at, last_active_at, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", contacts_data,
        )
        store._conn.executemany(
            "INSERT INTO journeys(journey_id, contact_id, persona_id, funnel_stage, "
            "intimacy_score, engagement_score, readiness_score, intimacy_updated_at, "
            "snapshot_refreshed_at, created_at, updated_at, context_snapshot_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", journeys_data,
        )
        store._conn.executemany(
            "INSERT INTO channel_identities(channel_identity_id, contact_id, channel, "
            "account_id, external_id, direction, linked_at, linked_via, "
            "attribution_confidence, display_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", cis_data,
        )
        store._conn.executemany(
            "INSERT INTO journey_events(event_id, journey_id, trace_id, event_type, "
            "payload_json, ts) VALUES (?, ?, ?, ?, ?, ?)", events_data,
        )
        store._conn.commit()

    app = FastAPI()
    register_contacts_routes(
        app, api_auth=lambda: None, contacts_store=store, merge_service=merge,
        intimacy_engine=intim, gateway=gateway,
    )
    tc = TestClient(app)
    tc.store = store  # type: ignore[attr-defined]
    yield tc
    store.close()


def test_trend_cold_under_2s(big_client):
    """SLO: 1000 contacts × 30 天 cold-path < 2s"""
    _intimacy_trend_cache_clear()
    t0 = time.monotonic()
    r = big_client.get("/api/relations/intimacy-trend?days=30&top_n=1000")
    elapsed = time.monotonic() - t0
    assert r.status_code == 200
    body = r.json()
    assert body["sample_size"] == 1000
    assert len(body["series"]) == 30
    assert elapsed < 2.0, f"trend cold path 太慢: {elapsed:.3f}s"
    print(f"\n[BENCH] trend cold (1000 × 30): {elapsed*1000:.0f}ms")


def test_trend_hot_under_50ms(big_client):
    """SLO: 同 key 第二次请求命中 cache < 50ms"""
    big_client.get("/api/relations/intimacy-trend?days=30&top_n=1000")  # 预热
    t0 = time.monotonic()
    r = big_client.get("/api/relations/intimacy-trend?days=30&top_n=1000")
    elapsed = time.monotonic() - t0
    assert r.status_code == 200
    assert elapsed < 0.05, f"trend hot path（cache hit）太慢: {elapsed*1000:.0f}ms"
    print(f"\n[BENCH] trend hot (cache hit): {elapsed*1000:.1f}ms")


def test_history_60d_under_200ms(big_client):
    """SLO: 单 journey 60 天 history < 200ms"""
    # 取第一个 journey
    with big_client.store._lock:
        row = big_client.store._conn.execute(
            "SELECT journey_id FROM journeys LIMIT 1"
        ).fetchone()
    jid = row[0]
    t0 = time.monotonic()
    r = big_client.get(f"/api/journeys/{jid}/intimacy-history?days=60")
    elapsed = time.monotonic() - t0
    assert r.status_code == 200
    assert elapsed < 0.2, f"history 60 天太慢: {elapsed*1000:.0f}ms"
    print(f"\n[BENCH] history (60d, ~60 events): {elapsed*1000:.1f}ms")
