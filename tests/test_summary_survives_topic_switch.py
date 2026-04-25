"""Phase 2 — 验证 topic-switch 不再清空 `_conversation_summary`（保留摘要承载长期事实）。

用源文件文本核验：line 1219-1226 段（话题切换清理块）不应再含 pop("_conversation_summary"...)。
配合 test_summary_llm_path.py 的运行时单元测试，已两层覆盖。
"""

from __future__ import annotations

from pathlib import Path


_SKILL_MANAGER = Path(__file__).resolve().parent.parent / "src" / "skills" / "skill_manager.py"


def test_topic_switch_block_no_longer_pops_summary():
    src = _SKILL_MANAGER.read_text(encoding="utf-8")
    # 找到话题切换的清理块（以 _intent_chain = [intent] 锚定）
    anchor = src.find('_intent_chain"] = [intent]')
    assert anchor != -1, "找不到 topic-switch 清理块的锚点"

    # 看锚点上方 200 字符（清理列表所在 window）
    window = src[max(0, anchor - 400): anchor]
    assert 'pop("_conversation_summary"' not in window, \
        "Phase 2 要求保留摘要：topic-switch 不应再 pop _conversation_summary"


def test_topic_switch_block_still_clears_history_and_chain():
    """保留摘要的同时，对话历史 / intent_chain 仍应被清，避免话题污染。"""
    src = _SKILL_MANAGER.read_text(encoding="utf-8")
    anchor = src.find('_intent_chain"] = [intent]')
    assert anchor != -1
    window = src[max(0, anchor - 400): anchor + 200]
    assert '_conversation_history"] = []' in window
    assert 'pop("last_reply"' in window
    assert 'pop("_chain_pattern"' in window
