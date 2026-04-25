"""端到端集成测试：真 SQLite + 真 ContactGateway + 真 GatewayContactHooks + 真 PortraitExtractor。

不 mock store / gateway / hooks / extractor — 只 mock AIClient（避免真调 LLM）。
覆盖：
- runner-style hooks.on_message 调用 → journey_events 入库
- PortraitExtractor.should_refresh / extract_and_persist 端到端调用 → snapshot 真写入
- render_block 渲染真 snapshot JSON → 含日文画像
- AIClient._build_context_prompt 读 _contact_portrait_block → 注入 prompt 顶部

补 phase 0-2 全 mock unit 的跨模块集成 wire 验证。

优化 C：fixtures 已抽到 conftest.py，本文件只用：
  contacts_store / contacts_gateway / contacts_hooks / mock_ai_client_ja
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.contacts.portrait_extractor import PortraitExtractor, render_block


# ── E2E：日文消息 → 画像写入 ─────────────────────────────────


@pytest.mark.asyncio
async def test_e2e_ja_inbound_messages_to_portrait_snapshot(
    contacts_store, contacts_hooks, mock_ai_client_ja,
):
    """完整流程：5 条日文 inbound → hook 写库 → extract 抽画像 → 渲染 portrait block。"""

    # 1. 模拟 5 条日文消息进 hook（runner-style）
    ja_messages = [
        "こんにちは、調子はどうですか？",
        "今度の週末、東京に行く予定です",
        "おすすめのレストランがあれば教えてください",
        "料理が好きで、和食をよく作ります",
        "お時間があれば連絡してください",
    ]
    for msg in ja_messages:
        ctx = contacts_hooks.on_message(
            channel="messenger",
            account_id="bg_phone_2",
            external_id="さとう たかひろ",
            direction="in",
            text_preview=msg,
            display_name="さとう たかひろ",
            trace_id=f"e2e-{int(time.time())}",
        )
        assert ctx is not None
        assert ctx.contact.contact_id

    # 2. 验证 journey_events 入库
    contact_id = ctx.contact.contact_id
    journey = contacts_store.get_journey_by_contact(contact_id)
    assert journey is not None
    events = contacts_store.list_events(journey.journey_id, limit=20)
    msg_in_events = [e for e in events if e["event_type"] == "msg_in"]
    assert len(msg_in_events) == 5

    # 3. PortraitExtractor 应判定需要抽（snapshot 缺失）
    extractor = PortraitExtractor(contacts_store, mock_ai_client_ja)
    assert extractor.should_refresh(journey) is True

    # 4. 抽 + 写
    snapshot = await extractor.extract_and_persist(
        journey=journey, display_name="さとう たかひろ"
    )
    assert snapshot is not None
    assert snapshot["language"] == "ja"
    assert snapshot["tone"] == "casual_friendly"
    assert "_extracted_at" in snapshot
    assert snapshot["_msg_count"] == 5

    # 5. mock AI 真的被调用且 prompt 含日文消息
    mock_ai_client_ja.chat.assert_awaited_once()
    call_args = mock_ai_client_ja.chat.call_args
    prompt = call_args[0][0][0]["content"]
    assert "こんにちは" in prompt
    assert "東京" in prompt
    # display_name 也注入了
    assert "さとう" in prompt

    # 6. 重新读 store，确认 snapshot 真持久化
    journey_reloaded = contacts_store.get_journey_by_contact(contact_id)
    assert journey_reloaded.context_snapshot_json
    saved = json.loads(journey_reloaded.context_snapshot_json)
    assert saved["language"] == "ja"
    assert journey_reloaded.snapshot_refreshed_at > 0

    # 7. should_refresh 现在应为 False（刚抽完，无新消息）
    assert extractor.should_refresh(journey_reloaded) is False

    # 8. render_block 渲染出有用的 portrait block
    block = render_block(journey_reloaded.context_snapshot_json)
    assert "对话伙伴画像" in block
    assert "ja" in block
    assert "casual_friendly" in block
    assert "旅行" in block
    assert "日本在住" in block


# ── E2E：portrait block → AIClient prompt 注入 ──────────────


def test_e2e_portrait_block_flows_to_ai_client_prompt():
    """runner 把 render_block(snap) 渲染好塞 ctx → AIClient._build_context_prompt 注入到顶部。"""
    from src.ai.ai_client import AIClient

    class _Cfg:
        config_path = None
        config = {"web_admin": {"site_name": "T"}, "ai": {}}
        def get_ai_config(self):
            return {}

    snap_json = json.dumps({
        "language": "ja",
        "tone": "casual_friendly",
        "interests": ["旅行"],
        "recent_topics": ["週末"],
        "key_facts": ["日本在住"],
        "intimacy_signal": "warming",
    }, ensure_ascii=False)

    portrait = render_block(snap_json)
    assert portrait

    client = AIClient(_Cfg())
    ctx = {
        "channel": "messenger_rpa",
        "_contact_portrait_block": portrait,
    }
    prompt = client._build_context_prompt(ctx)
    assert "对话伙伴画像" in prompt
    assert "ja" in prompt
    assert "日本在住" in prompt


# ── E2E：增量 inbound → 触发再抽（refresh 路径） ───────────


@pytest.mark.asyncio
async def test_e2e_incremental_inbound_triggers_refresh(
    contacts_store, contacts_hooks, mock_ai_client_ja,
):
    """首次抽完后，再来 ≥ N 条新 inbound 应再次触发 should_refresh=True。"""
    extractor = PortraitExtractor(
        contacts_store, mock_ai_client_ja,
        refresh_every_n_inbound=3,  # 测试用低阈值
    )

    # 首批 3 条
    for i in range(3):
        contacts_hooks.on_message(
            channel="messenger", account_id="acc1", external_id="user_a",
            direction="in", text_preview=f"first batch msg {i} です",
            display_name="user_a", trace_id=f"t-{i}",
        )

    # 重新拿 contact_id（hooks.on_message 已返回，从 store 反查）
    last_ctx = contacts_hooks.on_message(
        channel="messenger", account_id="acc1", external_id="user_a",
        direction="in", text_preview="probe", display_name="user_a", trace_id="probe",
    )
    journey = contacts_store.get_journey_by_contact(last_ctx.contact.contact_id)
    assert extractor.should_refresh(journey) is True

    snap1 = await extractor.extract_and_persist(journey=journey, display_name="user_a")
    assert snap1 is not None
    journey = contacts_store.get_journey_by_contact(journey.contact_id)
    refreshed_at_1 = journey.snapshot_refreshed_at

    # 同秒内的事件 ts 与 refreshed_at 相同会被判 == 不算新；隔开 1 秒以模拟真实场景
    time.sleep(1.1)

    # 紧随其后再 1 条不应触发（< N=3）
    contacts_hooks.on_message(
        channel="messenger", account_id="acc1", external_id="user_a",
        direction="in", text_preview="just 1 new",
        display_name="user_a", trace_id="t-new1",
    )
    assert extractor.should_refresh(journey) is False

    # 再补 2 条达到 3 → 触发
    for i in range(2):
        contacts_hooks.on_message(
            channel="messenger", account_id="acc1", external_id="user_a",
            direction="in", text_preview=f"new msg {i}",
            display_name="user_a", trace_id=f"t-new{i+2}",
        )
    journey = contacts_store.get_journey_by_contact(journey.contact_id)
    assert extractor.should_refresh(journey) is True

    # 抽第二次，refreshed_at 应推进
    snap2 = await extractor.extract_and_persist(journey=journey, display_name="user_a")
    assert snap2 is not None
    journey = contacts_store.get_journey_by_contact(journey.contact_id)
    assert journey.snapshot_refreshed_at >= refreshed_at_1


# ── E2E：多 contact 隔离（snapshot 不串）─────────────────────


@pytest.mark.asyncio
async def test_e2e_multiple_contacts_isolated_snapshots(
    contacts_store, contacts_hooks,
):
    """不同 contact 各自独立 snapshot，不互相覆盖。"""
    ai_a = MagicMock()
    ai_a.chat = AsyncMock(return_value='{"language":"ja","tone":"calm"}')
    ai_b = MagicMock()
    ai_b.chat = AsyncMock(return_value='{"language":"en","tone":"playful"}')

    # 两个不同 contact 各 3 条入站
    cid_jp = cid_en = None
    for i in range(3):
        ctx_jp = contacts_hooks.on_message(
            channel="messenger", account_id="acc1", external_id="user_jp",
            direction="in", text_preview=f"おはよう {i}",
            display_name="user_jp", trace_id=f"a{i}",
        )
        ctx_en = contacts_hooks.on_message(
            channel="messenger", account_id="acc1", external_id="user_en",
            direction="in", text_preview=f"morning {i}",
            display_name="user_en", trace_id=f"b{i}",
        )
        cid_jp = ctx_jp.contact.contact_id
        cid_en = ctx_en.contact.contact_id

    assert cid_jp != cid_en

    journey_jp = contacts_store.get_journey_by_contact(cid_jp)
    journey_en = contacts_store.get_journey_by_contact(cid_en)

    ext_jp = PortraitExtractor(contacts_store, ai_a)
    ext_en = PortraitExtractor(contacts_store, ai_b)
    await ext_jp.extract_and_persist(journey=journey_jp, display_name="user_jp")
    await ext_en.extract_and_persist(journey=journey_en, display_name="user_en")

    journey_jp_r = contacts_store.get_journey_by_contact(cid_jp)
    journey_en_r = contacts_store.get_journey_by_contact(cid_en)
    snap_jp = json.loads(journey_jp_r.context_snapshot_json)
    snap_en = json.loads(journey_en_r.context_snapshot_json)
    assert snap_jp["language"] == "ja"
    assert snap_en["language"] == "en"
