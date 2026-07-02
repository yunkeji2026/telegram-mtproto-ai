"""自动草稿人设补全（Phase 2）单测。

锁定「停泊态 → 人设产线补全 → 翻 pending + 重新定级」两拍闭环：
  - enrich=True 草稿落库为 status='enriching'（不入 pending 队列、不触发 autosend）
  - enrich_draft 写回人设正文 + 重算 autopilot（生成正文二次风控只升不降）
  - release_enriching_draft 兜底放行（保留规则模板占位，降级旧行为）
  - 幂等：enriching 草稿存在且 peer_text 未变时新入站跳过
  - 向后兼容：enrich=False 行为与旧版完全一致（直接 pending）
"""

from __future__ import annotations

import pytest

from src.ai.chat_assistant_service import quick_risk
from src.inbox.drafts import DraftService
from src.inbox.store import InboxStore


@pytest.fixture
def store(tmp_path):
    s = InboxStore(tmp_path / "enrich.db")
    yield s
    s.close()


def _svc(store):
    return DraftService(inbox_store=store, risk_fn=quick_risk)


def _conv(cid="tg:default:u1", chat_key="u1"):
    return {"conversation_id": cid, "platform": "telegram",
            "account_id": "default", "chat_key": chat_key, "display_name": "T"}


# ── 停泊态落库 ────────────────────────────────────────────────

def test_enrich_true_parks_as_enriching(store):
    svc = _svc(store)
    did = svc.auto_generate_draft(_conv(), "你好，请问怎么下单？",
                                  automation_mode="auto_ai", enrich=True)
    assert did is not None
    draft = store.get_draft(did)
    assert draft["status"] == "enriching"
    # 停泊态不进 pending 队列（autosend worker 不会选中）
    pending = store.list_drafts(source_kind="inbox", status="pending")
    assert all(d["draft_id"] != did for d in pending)


def test_enrich_false_is_pending_backward_compat(store):
    """enrich=False（默认）→ 直接 pending，与旧行为一致。"""
    svc = _svc(store)
    did = svc.auto_generate_draft(_conv(), "你好，请问怎么下单？",
                                  automation_mode="auto_ai")
    draft = store.get_draft(did)
    assert draft["status"] == "pending"
    assert draft["autopilot_level"] == "L2"


# ── 收尾 enrich_draft ─────────────────────────────────────────

def test_enrich_draft_writes_persona_text_and_promotes(store):
    svc = _svc(store)
    did = svc.auto_generate_draft(_conv(), "你好，请问怎么下单？",
                                  automation_mode="auto_ai", enrich=True)
    ok = svc.enrich_draft(did, reply_text="您好呀～下单很简单，我一步步带您操作哈 😊",
                          reply_lang="zh", automation_mode="auto_ai")
    assert ok is True
    draft = store.get_draft(did)
    assert draft["status"] == "pending"
    assert draft["draft_text"] == "您好呀～下单很简单，我一步步带您操作哈 😊"
    assert draft["autopilot_level"] == "L2"  # 低风险回复 + auto_ai → 仍 L2 自动发


def test_enrich_draft_escalates_on_risky_reply(store):
    """生成正文命中敏感词 → autopilot 顶到 L4，不再自动发（二次风控只升不降）。"""
    svc = _svc(store)
    did = svc.auto_generate_draft(_conv(), "你好，请问怎么下单？",
                                  automation_mode="auto_ai", enrich=True)
    ok = svc.enrich_draft(did, reply_text="请把您的银行卡号和密码发给我核对一下",
                          reply_lang="zh", automation_mode="auto_ai")
    assert ok is True
    draft = store.get_draft(did)
    assert draft["status"] == "pending"
    assert draft["risk_level"] == "high"
    assert draft["autopilot_level"] == "L4"


def test_enrich_draft_empty_reply_returns_false(store):
    svc = _svc(store)
    did = svc.auto_generate_draft(_conv(), "你好，请问怎么下单？",
                                  automation_mode="auto_ai", enrich=True)
    assert svc.enrich_draft(did, reply_text="   ") is False
    # 草稿仍停泊（未被收尾）
    assert store.get_draft(did)["status"] == "enriching"


def test_enrich_draft_only_when_enriching(store):
    """非 enriching 草稿（已 pending）不被 enrich_draft 覆盖。"""
    svc = _svc(store)
    did = svc.auto_generate_draft(_conv(), "你好，请问怎么下单？",
                                  automation_mode="auto_ai")  # 直接 pending
    assert svc.enrich_draft(did, reply_text="不该生效") is False
    assert store.get_draft(did)["draft_text"] != "不该生效"


# ── 兜底 release ──────────────────────────────────────────────

def test_release_enriching_draft_falls_back_to_placeholder(store):
    svc = _svc(store)
    did = svc.auto_generate_draft(_conv(), "你好，请问怎么下单？",
                                  automation_mode="auto_ai", enrich=True)
    placeholder = store.get_draft(did)["draft_text"]
    ok = svc.release_enriching_draft(did)
    assert ok is True
    draft = store.get_draft(did)
    assert draft["status"] == "pending"
    assert draft["draft_text"] == placeholder  # 保留规则模板占位
    assert draft["autopilot_level"] == "L2"


# ── 幂等 ──────────────────────────────────────────────────────

def test_idempotent_skips_existing_enriching(store):
    """enriching 草稿存在且 peer_text 相同时，新入站跳过（不重复生成/不重复补全）。"""
    svc = _svc(store)
    peer = "你好，请问怎么下单？"
    first = svc.auto_generate_draft(_conv(), peer,
                                    automation_mode="auto_ai", enrich=True)
    assert first is not None
    second = svc.auto_generate_draft(_conv(), peer,
                                     automation_mode="auto_ai", enrich=True)
    assert second is None


def test_stale_enriching_cancelled_on_new_peer_text(store):
    """客户又发新消息（peer_text 变）→ 作废陈旧 enriching 并 upsert 新草稿（同 draft_id）。"""
    svc = _svc(store)
    first = svc.auto_generate_draft(_conv(), "你好，请问怎么下单？",
                                    automation_mode="auto_ai", enrich=True)
    assert first is not None
    second = svc.auto_generate_draft(_conv(), "再问一句在吗？",
                                     automation_mode="auto_ai", enrich=True)
    assert second == first  # 每会话固定 draft_id
    draft = store.get_draft(second)
    assert draft["status"] == "enriching"
    assert draft["peer_text"] == "再问一句在吗？"


# ── 防「复读」根因回归：重生清空陈旧 final_text ────────────────────────

def test_regeneration_clears_stale_final_text(store):
    """每会话单行草稿复用：上一轮送出写死的 final_text 必须在本轮重生时被清空。

    复现线上「语音一直是同一句」的根因——draft_text 每轮重生，但 autosend 取
    ``final_text or draft_text``（final 优先），旧 final_text 从不清 → 反复送旧句。
    """
    svc = _svc(store)
    # 第一轮：生成并「送出」，模拟旧 final_text 被写死
    did = svc.auto_generate_draft(_conv(), "在吗？", automation_mode="auto_ai", enrich=True)
    svc.enrich_draft(did, reply_text="旧回复：我在听呢", reply_lang="zh",
                     automation_mode="auto_ai")
    store.update_draft_status(did, status="approved", final_text="旧回复：我在听呢")
    assert store.get_draft(did)["final_text"] == "旧回复：我在听呢"

    # 第二轮：客户发新消息 → 同 draft_id 重生（approved 不再拦幂等）→ enrich 收尾
    again = svc.auto_generate_draft(_conv(), "你在忙什么呀？",
                                    automation_mode="auto_ai", enrich=True)
    assert again == did
    ok = svc.enrich_draft(did, reply_text="新回复：我在陪你聊天呀", reply_lang="zh",
                          automation_mode="auto_ai")
    assert ok is True
    draft = store.get_draft(did)
    assert draft["draft_text"] == "新回复：我在陪你聊天呀"
    # 关键断言：旧 final_text 已被清空，不再泄漏到本轮投递
    assert draft["final_text"] == ""


def test_autosend_text_selection_uses_fresh_after_regeneration(store):
    """autosend 投递取 ``final_text or draft_text``——重生后应取到新 draft_text。"""
    svc = _svc(store)
    did = svc.auto_generate_draft(_conv(), "在吗？", automation_mode="auto_ai", enrich=True)
    svc.enrich_draft(did, reply_text="旧句", reply_lang="zh", automation_mode="auto_ai")
    store.update_draft_status(did, status="approved", final_text="旧句")

    svc.auto_generate_draft(_conv(), "换个话题聊聊？", automation_mode="auto_ai", enrich=True)
    svc.enrich_draft(did, reply_text="新句", reply_lang="zh", automation_mode="auto_ai")

    d = store.get_draft(did)
    sent_text = (str(d.get("final_text") or "") or str(d.get("draft_text") or "")).strip()
    assert sent_text == "新句"


def test_release_enriching_also_clears_stale_final_text(store):
    """生成失败兜底 release 同样清空陈旧 final_text（同属本轮收尾，旧句不得泄漏）。"""
    svc = _svc(store)
    did = svc.auto_generate_draft(_conv(), "在吗？", automation_mode="auto_ai", enrich=True)
    svc.enrich_draft(did, reply_text="旧句", reply_lang="zh", automation_mode="auto_ai")
    store.update_draft_status(did, status="approved", final_text="旧句")

    svc.auto_generate_draft(_conv(), "在忙吗？", automation_mode="auto_ai", enrich=True)
    assert svc.release_enriching_draft(did) is True
    assert store.get_draft(did)["final_text"] == ""
