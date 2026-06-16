"""AutoClaimWorker（auto_assign 自动认领执行端）测试。

锁定安全契约：默认关、仅认领等待回复的未认领会话、不抢占、match_language 协同、
热开关（按 auto_claim.enabled 每 tick 重读）。
"""

import asyncio
import time
from types import SimpleNamespace

from src.inbox.models import InboxConversation, InboxMessage
from src.inbox.store import InboxStore
from src.workspace.auto_claim_worker import AutoClaimWorker


def _cfg(*, auto_claim=True, match_language=True):
    return SimpleNamespace(config={"workspace": {"auto_assign": {
        "enabled": True,
        "match_language": match_language,
        "auto_claim": {"enabled": auto_claim, "active_within_sec": 0},
    }}})


def _seed_waiting(store, cid, chat_key, language, *, direction="in", ts=100.0):
    store.upsert_conversation(InboxConversation(
        conversation_id=cid, platform="line", account_id="acc", chat_key=chat_key,
        display_name="C", language=language, last_text="hi", last_ts=ts))
    store.ingest_message(InboxMessage(
        conversation_id=cid, platform_msg_id=f"{chat_key}-m1",
        direction=direction, text="hi", ts=ts))


def _worker(store, cfg):
    w = AutoClaimWorker(inbox_store=store, config_manager=cfg)
    return w, w._service()


def test_auto_claim_claims_waiting_unclaimed(tmp_path):
    store = InboxStore(tmp_path / "i.db")
    _seed_waiting(store, "line:acc:c1", "c1", "ja")
    store.upsert_agent_presence("a1", display_name="A", status="online")
    store.set_agent_languages("a1", "ja")
    w, svc = _worker(store, _cfg())
    w._do_claims(svc)
    claim = store.get_conversation_claim("line:acc:c1")
    assert claim and claim["agent_id"] == "a1"
    assert w.total_claimed == 1
    assert w.total_lang_matched == 1


def test_auto_claim_prefers_language_speaker(tmp_path):
    store = InboxStore(tmp_path / "i.db")
    _seed_waiting(store, "line:acc:c1", "c1", "ja")
    # a2 负载更低但不会日语；a1 会日语 → 应派给 a1
    store.upsert_agent_presence("a1", display_name="A", status="online")
    store.upsert_agent_presence("a2", display_name="B", status="online")
    store.set_agent_languages("a1", "ja")
    store.set_conversation_claim("line:acc:other", "a1", ttl_sec=3600)  # a1 已有 1 负载
    w, svc = _worker(store, _cfg())
    w._do_claims(svc)
    claim = store.get_conversation_claim("line:acc:c1")
    assert claim["agent_id"] == "a1"
    assert w.total_lang_matched == 1


def test_auto_claim_skips_non_waiting(tmp_path):
    """末条出向（不在等待回复）→ 不自动认领。"""
    store = InboxStore(tmp_path / "i.db")
    _seed_waiting(store, "line:acc:c1", "c1", "ja", direction="out")
    store.upsert_agent_presence("a1", display_name="A", status="online")
    store.set_agent_languages("a1", "ja")
    w, svc = _worker(store, _cfg())
    w._do_claims(svc)
    assert store.get_conversation_claim("line:acc:c1") is None
    assert w.total_claimed == 0


def test_auto_claim_does_not_steal_claimed(tmp_path):
    """已被他人认领的会话 → 不抢占。"""
    store = InboxStore(tmp_path / "i.db")
    _seed_waiting(store, "line:acc:c1", "c1", "ja")
    store.set_conversation_claim("line:acc:c1", "human", ttl_sec=3600)
    store.upsert_agent_presence("a1", display_name="A", status="online")
    store.set_agent_languages("a1", "ja")
    w, svc = _worker(store, _cfg())
    w._do_claims(svc)
    claim = store.get_conversation_claim("line:acc:c1")
    assert claim["agent_id"] == "human"   # 未被抢
    assert w.total_claimed == 0


def test_auto_claim_disabled_is_noop(tmp_path):
    """auto_claim.enabled=false → tick 空转，不认领（热开关）。"""
    store = InboxStore(tmp_path / "i.db")
    _seed_waiting(store, "line:acc:c1", "c1", "ja")
    store.upsert_agent_presence("a1", display_name="A", status="online")
    store.set_agent_languages("a1", "ja")
    w = AutoClaimWorker(inbox_store=store, config_manager=_cfg(auto_claim=False))
    asyncio.run(w._tick())
    assert store.get_conversation_claim("line:acc:c1") is None
    assert w.total_claimed == 0


def test_auto_claim_fallback_when_no_speaker(tmp_path):
    """无人会该语言 → 仍认领给在线坐席（有人接 > 没人接），matched_language=False。"""
    store = InboxStore(tmp_path / "i.db")
    _seed_waiting(store, "line:acc:c1", "c1", "ja")
    store.upsert_agent_presence("a1", display_name="A", status="online")
    store.set_agent_languages("a1", "en")  # 不会 ja
    w, svc = _worker(store, _cfg())
    w._do_claims(svc)
    claim = store.get_conversation_claim("line:acc:c1")
    assert claim["agent_id"] == "a1"
    assert w.total_claimed == 1
    assert w.total_lang_matched == 0


def test_status_snapshot_shape(tmp_path):
    store = InboxStore(tmp_path / "i.db")
    w = AutoClaimWorker(inbox_store=store, config_manager=_cfg())
    snap = w.status_snapshot()
    assert snap["running"] is False
    assert "total_claimed" in snap and "total_lang_matched" in snap


def test_auto_claim_daily_roundtrip(tmp_path):
    """record_auto_claim → get_auto_claim_stats：累计、命中、语言分布、趋势。"""
    store = InboxStore(tmp_path / "i.db")
    store.record_auto_claim(matched=True, lang="ja")
    store.record_auto_claim(matched=True, lang="ja")
    store.record_auto_claim(matched=False, lang="")  # 兜底派单，不计命中/分布
    stats = store.get_auto_claim_stats(since_ts=time.time() - 86400)
    assert stats["claimed"] == 3
    assert stats["lang_matched"] == 2
    assert stats["by_lang"] == {"ja": 2}
    assert sum(t["claimed"] for t in stats["trend"]) == 3


def test_auto_claim_worker_persists_daily(tmp_path):
    """worker 每次成功派单写入按日表（与 status_snapshot 进程累计并行）。"""
    store = InboxStore(tmp_path / "i.db")
    _seed_waiting(store, "line:acc:c1", "c1", "ja")
    store.upsert_agent_presence("a1", display_name="A", status="online")
    store.set_agent_languages("a1", "ja")  # 语言命中
    w, svc = _worker(store, _cfg())
    w._do_claims(svc)
    stats = store.get_auto_claim_stats(since_ts=time.time() - 86400)
    assert stats["claimed"] == 1
    assert stats["lang_matched"] == 1
    assert stats["by_lang"] == {"ja": 1}


def test_workspace_base_template_has_auto_claimed_toast():
    """坐席端 SSE 处理器须为 auto_claimed 派单事件提供专属 toast（仅被分配人可见）。"""
    with open("src/web/templates/workspace_base.html", encoding="utf-8") as f:
        html = f.read()
    assert "auto_claimed" in html
    assert "matched_language" in html
    # 仅被分配坐席收到：必须按 MY_AGENT_ID 过滤
    assert "_acClaim.agent_id===MY_AGENT_ID" in html
