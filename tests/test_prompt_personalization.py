"""Phase 1 — system prompt 个性化注入测试。

验证 AIClient._build_context_prompt 把 _contact_portrait_block 注入到 prompt 顶部。
"""

from __future__ import annotations

from src.ai.ai_client import AIClient


class _Cfg:
    config_path = None
    config = {"web_admin": {"site_name": "T"}, "ai": {}}

    def get_ai_config(self):
        return {}


def test_build_context_prompt_injects_portrait_when_present():
    client = AIClient(_Cfg())
    portrait = (
        "【对话伙伴画像 · 内部参考勿提及】\n"
        "- 主要语言：ja\n"
        "- 语气偏好：casual_friendly\n"
        "- 已知兴趣：旅行、料理\n"
        "- 关键事实：日本在住"
    )
    ctx = {
        "channel": "messenger_rpa",
        "_contact_portrait_block": portrait,
    }
    out = client._build_context_prompt(ctx)
    assert "对话伙伴画像" in out
    assert "ja" in out
    assert "日本在住" in out


def test_build_context_prompt_no_portrait_block_no_inject():
    client = AIClient(_Cfg())
    out = client._build_context_prompt({"channel": "messenger_rpa"})
    assert "对话伙伴画像" not in out


def test_build_context_prompt_empty_portrait_string_no_inject():
    client = AIClient(_Cfg())
    out = client._build_context_prompt({
        "channel": "messenger_rpa",
        "_contact_portrait_block": "   ",
    })
    assert "对话伙伴画像" not in out


def test_portrait_block_appears_before_other_channel_hints():
    """画像块应出现在其他渠道 hint 之前（更高优先级 / 角色锚点）。"""
    client = AIClient(_Cfg())
    portrait = "【对话伙伴画像 · 内部参考勿提及】\n- 主要语言：ja"
    line_hint = "测试 LINE 风格 hint"
    ctx = {
        "channel": "line_rpa",
        "_contact_portrait_block": portrait,
        "line_rpa_style_hint": line_hint,
    }
    out = client._build_context_prompt(ctx)
    portrait_pos = out.find("对话伙伴画像")
    line_pos = out.find(line_hint)
    assert portrait_pos != -1 and line_pos != -1
    assert portrait_pos < line_pos
