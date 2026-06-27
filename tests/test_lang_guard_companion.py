"""陪伴(domain=conversion)模式语言守卫回归。

历史 bug：`_guard_reply_language` 对 companion 模式整段早退，导致英文客户在
「中文人设 + 长段中文历史」惯性下仍被回复纯中文（线上实测：客户切英文后机器人
连续 3 条回中文，客户抱怨 "Why don't you speak English anymore?"）。

修复后：companion 也走 `_reply_lang_mismatch` 兜底——仅当回复「明显不符」
（目标英文却是 CJK 占绝对多数）才纠正，对中文会话与正常目标语回复零误伤。
"""

import pytest

from src.ai.ai_client import AIClient


def _bare_client(translate_to: str = ""):
    """绕过 __init__ 造一个只够测守卫的轻量 AIClient。

    守卫(修复后)只依赖 logger / _LANG_NAMES(类属性) / generate_reply /
    _reply_lang_mismatch，不再读 self.config，故可极简构造。
    """
    obj = AIClient.__new__(AIClient)
    # logger 走 LoggerMixin 的惰性 property（无需 __init__）。
    calls = {"n": 0}

    async def _fake_generate_reply(prompt, context=None, **kw):
        calls["n"] += 1
        return translate_to

    obj.generate_reply = _fake_generate_reply  # type: ignore[assignment]
    return obj, calls


# ── _reply_lang_mismatch：纯静态启发式 ────────────────────────────────────

def test_mismatch_pure_chinese_to_english_is_mismatch():
    zh = "哈哈你也太可爱了吧，下次记得拍给我看看，让我也解解馋啦啦啦啦啦啦"
    assert AIClient._reply_lang_mismatch(zh, "en") is True


def test_mismatch_mostly_english_with_few_cjk_is_ok():
    en = "Hey there! Just got back from a walk in the Bay Area, it was lovely."
    assert AIClient._reply_lang_mismatch(en, "en") is False


def test_mismatch_zh_target_never_mismatch():
    assert AIClient._reply_lang_mismatch("anything 任何", "zh") is False


# ── _guard_reply_language：companion 不再整段跳过 ──────────────────────────

@pytest.mark.asyncio
async def test_companion_english_target_chinese_reply_gets_corrected():
    """核心回归：英文客户、纯中文回复 → 守卫纠正为英文。"""
    corrected_en = "Haha you're so cute, send me a photo next time so I can drool too!"
    client, calls = _bare_client(translate_to=corrected_en)
    zh_reply = "哈哈你也太可爱了吧，下次记得拍给我看看，让我也解解馋啦啦啦啦啦啦"
    out = await client._guard_reply_language(zh_reply, {"reply_lang": "en"})
    assert out == corrected_en
    assert calls["n"] == 1, "应触发一次翻译纠正"


@pytest.mark.asyncio
async def test_zh_reply_lang_short_circuits_no_correction():
    client, calls = _bare_client(translate_to="should-not-be-used")
    out = await client._guard_reply_language("随便中文", {"reply_lang": "zh"})
    assert out == "随便中文"
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_english_reply_for_english_target_unchanged():
    client, calls = _bare_client(translate_to="should-not-be-used")
    en_reply = "Sure! Let's meet on Saturday afternoon, sounds good to me."
    out = await client._guard_reply_language(en_reply, {"reply_lang": "en"})
    assert out == en_reply
    assert calls["n"] == 0, "未触发纠正（无明显不符）"


@pytest.mark.asyncio
async def test_skip_flag_bypasses_guard():
    client, calls = _bare_client(translate_to="x")
    zh = "哈哈你也太可爱了吧，下次记得拍给我看看，让我也解解馋啦啦啦啦啦啦"
    out = await client._guard_reply_language(
        zh, {"reply_lang": "en", "_skip_lang_guard": True}
    )
    assert out == zh
    assert calls["n"] == 0


# ── 生成端 prompt：companion 也要硬禁中文 + 反历史动量 ─────────────────────

class _ConvCfg:
    """domain=conversion(companion) 的最小 config 壳。"""

    config_path = None
    config = {"domain": "conversion", "web_admin": {"site_name": "T"}, "ai": {}}

    def get_ai_config(self):
        return {}


def test_companion_en_prompt_hard_no_chinese_and_anti_momentum():
    """companion + reply_lang=en：system prompt 必含『硬禁中文字符』(修复前 companion
    en 缺失，是英文客户被回中文的源头之一) + 『跟最新消息切语言』反动量指令。"""
    client = AIClient(_ConvCfg())
    prompt = client._build_system_instruction({"reply_lang": "en"})
    assert "LANGUAGE RULE" in prompt
    assert "DO NOT output any Chinese characters" in prompt
    assert "SWITCH to English NOW" in prompt


def test_companion_zh_prompt_keeps_no_top_priority_block():
    """reply_lang=zh：不注入强制 LANGUAGE RULE（保持既有通用多语言规则，零回归）。"""
    client = AIClient(_ConvCfg())
    prompt = client._build_system_instruction({"reply_lang": "zh"})
    assert "LANGUAGE RULE — TOP PRIORITY" not in prompt
