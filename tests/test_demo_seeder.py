"""C1-2 试用/Demo 测试：示例数据铺设 / 命名空间隔离清空 / 看板可见 / 路由。"""

from src.inbox.models import InboxConversation, InboxMessage
from src.inbox.store import InboxStore
from src.utils.demo_seeder import (
    DEMO_PREFIX,
    clear_demo,
    demo_status,
    seed_demo,
)


def test_seed_demo_populates_dashboards(tmp_path):
    """铺设后会话/消息/草稿处置非空，用量看板能读到数字。"""
    store = InboxStore(tmp_path / "inbox.db")
    res = seed_demo(store, days=14)
    assert res["ok"] is True
    assert res["conversations"] >= 5
    assert res["messages"] > 0
    assert res["draft_audits"] > 0
    # 用量看板读得到
    usage = store.get_usage_stats(0.0)
    assert usage["messages_total"] > 0
    assert usage["ai_calls"] > 0
    assert usage["active_agents"] >= 1  # demo 含坐席处置
    # 质量看板读得到（含多种处置）
    q = store.get_quality_stats(0.0)
    assert q["total"] > 0
    store.close()


def test_demo_status_reflects_presence(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    assert demo_status(store)["present"] is False
    seed_demo(store, days=7)
    st = demo_status(store)
    assert st["present"] is True
    assert st["counts"]["conversations"] >= 5
    store.close()


def test_clear_demo_only_removes_demo_namespace(tmp_path):
    """清空只删 demo: 命名空间，真实数据原样保留。"""
    store = InboxStore(tmp_path / "inbox.db")
    # 真实数据
    real_cid = "telegram:acc:real1"
    store.ingest_message(InboxMessage(
        conversation_id=real_cid, platform_msg_id="r1", direction="in",
        text="真实客户消息", ts=1000.0))
    store.upsert_conversation(InboxConversation(
        conversation_id=real_cid, platform="telegram", display_name="真实客户",
        last_text="真实客户消息", last_ts=1000.0))
    store.record_draft_audit("real_d1", action="autosend", autopilot_level="L2",
                             conversation_id=real_cid, ts=1000.0)
    # demo 数据
    seed_demo(store, days=7)
    assert demo_status(store)["present"] is True
    # 清空 demo
    out = clear_demo(store)
    assert out["ok"] is True
    assert demo_status(store)["present"] is False
    # 真实数据仍在
    assert store.count_demo(DEMO_PREFIX)["messages"] == 0
    msgs = store.list_messages(real_cid)
    assert any("真实客户消息" in (m.get("text") or "") for m in msgs)
    convs = {c["conversation_id"] for c in store.list_conversations(limit=50)}
    assert real_cid in convs
    store.close()


def test_seed_is_idempotent(tmp_path):
    """重复铺设不会翻倍（先清后铺）。"""
    store = InboxStore(tmp_path / "inbox.db")
    r1 = seed_demo(store, days=7)
    c1 = demo_status(store)["counts"]
    r2 = seed_demo(store, days=7)
    c2 = demo_status(store)["counts"]
    assert c1 == c2
    assert r1["conversations"] == r2["conversations"]
    store.close()


def test_demo_routes_registered():
    import inspect
    from src.web.routes import demo_routes
    src = inspect.getsource(demo_routes.register_demo_routes)
    assert "/api/admin/demo" in src
    assert "/api/admin/demo/seed" in src
    assert "/api/admin/demo/clear" in src
