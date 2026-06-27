"""自动草稿人设补全（Phase 2）单测。

锁定「停泊态 → 人设产线补全 → 翻 pending + 重新定级」两拍闭环：
  - enrich=True 草稿落库为 status='enriching'（不入 pending 队列、不触发 autosend）
  - enrich_draft 写回人设正文 + 重算 autopilot（生成正文二次风控只升不降）
  - release_enriching_draft 兜底放行（保留规则模板占位，降级旧行为）
  - 幂等：enriching 草稿存在时新入站跳过
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
    """enriching 草稿存在时，新入站消息跳过（不重复生成/不重复补全）。"""
    svc = _svc(store)
    first = svc.auto_generate_draft(_conv(), "你好，请问怎么下单？",
                                    automation_mode="auto_ai", enrich=True)
    assert first is not None
    second = svc.auto_generate_draft(_conv(), "再问一句在吗？",
                                     automation_mode="auto_ai", enrich=True)
    assert second is None
