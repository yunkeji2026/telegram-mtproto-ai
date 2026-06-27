"""共享人设回复生成器单测（Phase 1：生成产线收敛）。

锁定 ``src/inbox/persona_reply.py`` 契约：
  - normalize_history：方向归一 + 取最后入站文本 + 兜底
  - generate_persona_reply：主路径（skill_manager 人设产线）/ 兜底路径（仅 ai_client）/
    空上下文早退 / 译文附加
这些是 /api/desktop/smart-reply 与收件箱全自动草稿复用的同一条产线，必须稳定。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.ai.translation_service import TranslationService
from src.inbox.persona_reply import (
    generate_persona_reply,
    normalize_history,
    resolve_reply_language,
)


# ── resolve_reply_language：回复语言决策 + 短消息防误切 ─────────────────

def test_resolve_lang_explicit_wins():
    """手动 UI 选定的目标语最高优先，不被二次猜测覆盖。"""
    assert resolve_reply_language("你好", explicit="en") == "en"


def test_resolve_lang_follows_latest_inbound():
    """客户切到英文（足够长）→ 跟最新一条走英文。"""
    assert resolve_reply_language("What did you eat today?") == "en"


def test_resolve_lang_short_token_keeps_window_dominant():
    """中文会话里偶发一个英文短 token，不应误切英文（回落窗口主导语言）。"""
    history = [
        {"role": "user", "content": "你在干嘛呢"},
        {"role": "assistant", "content": "刚下班～"},
        {"role": "user", "content": "ok"},
    ]
    assert resolve_reply_language("ok", history) == "zh"


def test_resolve_lang_short_token_english_window_stays_english():
    """全英文会话里的短 token（yes）→ 仍英文，不被默认 zh 拽回。"""
    history = [
        {"role": "user", "content": "Where are you now?"},
        {"role": "assistant", "content": "On my way home."},
        {"role": "user", "content": "ok"},
    ]
    assert resolve_reply_language("ok", history) == "en"


def test_resolve_lang_empty_defaults():
    assert resolve_reply_language("", default="zh") == "zh"


# ── normalize_history ─────────────────────────────────────────

def test_normalize_history_roles_and_last_inbound():
    msgs = [
        {"direction": "in", "text": "你好"},
        {"direction": "out", "text": "您好，在的"},
        {"direction": "inbound", "text": "怎么下单？"},
        {"direction": "out", "text": ""},  # 空文本应被滤掉
    ]
    history, last_inbound = normalize_history(msgs)
    assert history == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "您好，在的"},
        {"role": "user", "content": "怎么下单？"},
    ]
    assert last_inbound == "怎么下单？"


def test_normalize_history_fallback_to_last_when_no_inbound():
    """全是出向消息时，last_inbound 回落到末条内容（兜底锚点）。"""
    msgs = [{"direction": "out", "text": "我先发一句"}]
    history, last_inbound = normalize_history(msgs)
    assert last_inbound == "我先发一句"


def test_normalize_history_ignores_non_dict_and_empty():
    history, last_inbound = normalize_history([None, {}, {"text": "  "}, "bad"])
    assert history == []
    assert last_inbound == ""


# ── 测试替身 ──────────────────────────────────────────────────

class _FakeAI:
    def __init__(self):
        self.last_ctx = None

    async def generate_reply_with_intent(self, *, user_message, intent,
                                         user_context, strategy_overrides=None):
        self.last_ctx = user_context
        return f"[人设]{user_message}"

    async def chat(self, prompt):
        return "[兜底]回复"


class _FakeSM:
    def __init__(self, ai):
        self.ai_client = ai

    def _recognize_intent(self, text):
        return "consult"

    def get_strategy_for_intent(self, intent, user_id):
        return ({"temperature": 0.7}, "sid-1")


class _FakeRes:
    ok = True

    def to_dict(self):
        return {"translated_text": "<译文>"}


class _FakeTranslation(TranslationService):
    def __init__(self):  # 不调父类，避免真实依赖
        pass

    async def translate(self, text, target_lang="", style=""):
        return _FakeRes()


def _app(**state):
    return SimpleNamespace(state=SimpleNamespace(**state))


# ── generate_persona_reply ───────────────────────────────────

@pytest.mark.asyncio
async def test_generate_persona_reply_main_path_uses_skill_manager():
    ai = _FakeAI()
    app = _app(skill_manager=_FakeSM(ai), ai_client=ai, kb_store=None,
               telegram_client=None, translation_service=None)
    history, last = normalize_history([{"direction": "in", "text": "怎么下单"}])
    out = await generate_persona_reply(
        app=app, platform="telegram", chat_key="room1",
        last_inbound=last, history=history,
    )
    assert out["ok"] is True
    assert out["reply"] == "[人设]怎么下单"
    assert out["intent"] == "consult"
    # 策略覆盖透传到 ctx 的 reply_strategy
    assert ai.last_ctx["_reply_strategy"] == {"temperature": 0.7}
    assert "persona_tier" in out


@pytest.mark.asyncio
async def test_generate_persona_reply_fallback_when_no_skill_manager():
    ai = _FakeAI()
    app = _app(skill_manager=None, ai_client=ai, kb_store=None,
               telegram_client=None, translation_service=None)
    history, last = normalize_history([{"direction": "in", "text": "在吗"}])
    out = await generate_persona_reply(
        app=app, platform="telegram", chat_key="r2",
        last_inbound=last, history=history,
    )
    assert out["ok"] is True
    assert out["reply"] == "[兜底]回复"


@pytest.mark.asyncio
async def test_generate_persona_reply_empty_context_returns_not_ok():
    app = _app(skill_manager=None, ai_client=_FakeAI())
    out = await generate_persona_reply(
        app=app, platform="telegram", chat_key="r3",
        last_inbound="", history=[],
    )
    assert out["ok"] is False
    assert out["reply"] == ""


@pytest.mark.asyncio
async def test_generate_persona_reply_appends_translation():
    ai = _FakeAI()
    app = _app(skill_manager=_FakeSM(ai), ai_client=ai, kb_store=None,
               telegram_client=None, translation_service=_FakeTranslation())
    history, last = normalize_history([{"direction": "in", "text": "hi"}])
    out = await generate_persona_reply(
        app=app, platform="telegram", chat_key="r4",
        last_inbound=last, history=history, target_lang="en",
    )
    assert out["ok"] is True
    assert out["translated"] == "<译文>"


@pytest.mark.asyncio
async def test_generate_persona_reply_no_ai_at_all_not_ok():
    """skill_manager 与 ai_client 皆缺 → 无回复，ok=False（不抛错）。"""
    app = _app(skill_manager=None, ai_client=None, telegram_client=None)
    history, last = normalize_history([{"direction": "in", "text": "测试"}])
    out = await generate_persona_reply(
        app=app, platform="telegram", chat_key="r5",
        last_inbound=last, history=history,
    )
    assert out["ok"] is False
    assert out["reply"] == ""


# ── 语言决策单一事实源（Phase 2 收敛）───────────────────────────

@pytest.mark.asyncio
async def test_persona_reply_auto_resolves_chinese():
    """不传 reply_lang/target_lang → 按客户消息自动决策；中文 → zh 并回写 out。"""
    ai = _FakeAI()
    app = _app(skill_manager=_FakeSM(ai), ai_client=ai, kb_store=None,
               telegram_client=None, translation_service=None)
    history, last = normalize_history([{"direction": "in", "text": "今天怎么下单呀"}])
    out = await generate_persona_reply(
        app=app, platform="telegram", chat_key="r6",
        last_inbound=last, history=history,
    )
    assert out["reply_lang"] == "zh"
    assert ai.last_ctx["reply_lang"] == "zh"


@pytest.mark.asyncio
async def test_persona_reply_auto_resolves_english():
    """英文客户、无显式语言 → 自动决策 en（修复前默认 zh 的核心隐患）。"""
    ai = _FakeAI()
    app = _app(skill_manager=_FakeSM(ai), ai_client=ai, kb_store=None,
               telegram_client=None, translation_service=None)
    history, last = normalize_history(
        [{"direction": "in", "text": "What did you eat today?"}]
    )
    out = await generate_persona_reply(
        app=app, platform="telegram", chat_key="r7",
        last_inbound=last, history=history,
    )
    assert out["reply_lang"] == "en"
    assert ai.last_ctx["reply_lang"] == "en"


@pytest.mark.asyncio
async def test_persona_reply_explicit_reply_lang_wins():
    """显式 reply_lang 最高优先，压过自动决策与 target_lang。"""
    ai = _FakeAI()
    app = _app(skill_manager=_FakeSM(ai), ai_client=ai, kb_store=None,
               telegram_client=None, translation_service=None)
    history, last = normalize_history([{"direction": "in", "text": "今天怎么下单"}])
    out = await generate_persona_reply(
        app=app, platform="telegram", chat_key="r8",
        last_inbound=last, history=history,
        reply_lang="ja", target_lang="en",
    )
    assert out["reply_lang"] == "ja"
    assert ai.last_ctx["reply_lang"] == "ja"


@pytest.mark.asyncio
async def test_persona_reply_target_lang_used_as_body_lang_when_no_reply_lang():
    """坐席选定 target_lang（无 reply_lang）→ 正文按 target_lang 生成。"""
    ai = _FakeAI()
    app = _app(skill_manager=_FakeSM(ai), ai_client=ai, kb_store=None,
               telegram_client=None, translation_service=None)
    history, last = normalize_history([{"direction": "in", "text": "今天怎么下单"}])
    out = await generate_persona_reply(
        app=app, platform="telegram", chat_key="r9",
        last_inbound=last, history=history, target_lang="en",
    )
    assert out["reply_lang"] == "en"
    assert ai.last_ctx["reply_lang"] == "en"


# ── 统一规则引擎接线（彻底对齐：优先 generate_inbox_draft）───────────────

class _FakeSMUnified(_FakeSM):
    """带统一引擎 + config 的 SM 替身。

    记录是否调用统一引擎 / 是否走了直连记忆写回，验证：
      - 默认走 generate_inbox_draft 并透传入参
      - 统一引擎已写记忆 → 不再触发 persona_reply 文末的 _episodic_memory_extract_async
    """

    def __init__(self, ai, *, unified=True):
        super().__init__(ai)
        self.config = SimpleNamespace(config={
            "inbox": {"auto_draft": {"unified_pipeline": unified}}
        })
        self.inbox_draft_calls = []
        self.episodic_writeback_calls = []

    async def generate_inbox_draft(self, *, text, chat_key, platform,
                                   history=None, persona_id="", reply_lang=""):
        self.inbox_draft_calls.append({
            "text": text, "chat_key": chat_key, "platform": platform,
            "persona_id": persona_id, "reply_lang": reply_lang,
            "history_len": len(history or []),
        })
        return {"reply": f"[统一]{text}", "intent": "unified_intent"}

    async def _episodic_memory_extract_async(self, *a, **k):
        self.episodic_writeback_calls.append((a, k))


@pytest.mark.asyncio
async def test_persona_reply_prefers_unified_engine():
    """SM 暴露 generate_inbox_draft 且 flag 开 → 走统一引擎，入参透传，记忆不双写。"""
    ai = _FakeAI()
    sm = _FakeSMUnified(ai)
    app = _app(skill_manager=sm, ai_client=ai, kb_store=None,
               telegram_client=None, translation_service=None)
    history, last = normalize_history([{"direction": "in", "text": "我叫Jun，记得吗"}])
    out = await generate_persona_reply(
        app=app, platform="telegram", chat_key="7340576921",
        last_inbound=last, history=history, persona_id="p1",
    )
    assert out["ok"] is True
    assert out["reply"] == "[统一]我叫Jun，记得吗"
    assert out["intent"] == "unified_intent"
    assert len(sm.inbox_draft_calls) == 1
    call = sm.inbox_draft_calls[0]
    assert call["chat_key"] == "7340576921"
    assert call["platform"] == "telegram"
    assert call["persona_id"] == "p1"
    # 统一引擎自带记忆写回 → persona_reply 不应再触发一次（避免双写）
    assert sm.episodic_writeback_calls == []
    # 统一引擎主路径不应回落到直连 ai_client
    assert ai.last_ctx is None


@pytest.mark.asyncio
async def test_persona_reply_unified_flag_off_falls_back_to_direct():
    """flag=false → 跳过统一引擎，回落直连产线（ai_client 被调用）。"""
    ai = _FakeAI()
    sm = _FakeSMUnified(ai, unified=False)
    app = _app(skill_manager=sm, ai_client=ai, kb_store=None,
               telegram_client=None, translation_service=None)
    history, last = normalize_history([{"direction": "in", "text": "在吗"}])
    out = await generate_persona_reply(
        app=app, platform="telegram", chat_key="r10",
        last_inbound=last, history=history,
    )
    assert out["ok"] is True
    assert out["reply"] == "[人设]在吗"        # 直连路径产物
    assert sm.inbox_draft_calls == []           # 未走统一引擎
    assert ai.last_ctx is not None              # 直连 ai_client 被调用
