"""锁定 `_suggestions` 的设计契约：建议正文恒为坐席母语（中文），不随 lang 本地化。

背景：`_suggestions(lang=...)` 的 lang 参数曾被怀疑是「忽略入参的缺陷」。实际是刻意
设计——建议是给中文坐席看的参考，面向客户的语言转换在发送时由 outbound 翻译完成
（/api/unified-inbox/send 的 target_lang/"auto" 或前端 #xlate-out）。本测试防止它被
误「修」成按 lang 产外语，从而让坐席看不懂自己要发的内容。
"""

import pytest

from src.ai.chat_assistant_service import _suggestions


def _has_cjk(s: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


@pytest.mark.parametrize("lang", ["zh", "en", "th", "ja", "ar", "vi", "unknown"])
@pytest.mark.parametrize(
    "intent", ["打招呼", "需要安抚", "短句接话", "其它意图"]
)
def test_suggestions_text_stays_agent_chinese_regardless_of_lang(lang, intent):
    out = _suggestions("hello there", lang=lang, intent=intent, emotion="平稳", risk="low")
    assert out, "至少应返回一条建议"
    # 契约：无论客户语言是什么，给坐席的建议正文都是中文
    assert _has_cjk(out[0].text), f"lang={lang} intent={intent} 建议正文应为坐席中文：{out[0].text!r}"


def test_high_risk_suggestions_also_chinese():
    out = _suggestions("转账给我", lang="en", intent="打招呼", emotion="平稳", risk="high")
    assert out
    # 高风险走人工审核/克制回应分支，同样是坐席中文
    assert _has_cjk(out[0].text)
