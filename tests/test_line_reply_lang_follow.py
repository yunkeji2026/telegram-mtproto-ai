"""LINE 回复语言：默认跟随客户语言（修正「外语客户被回中文」）。

优先级链：全局 force > per-chat forced_lang > 客户消息语言检测 > default_reply_lang。
本测试锁定新增的「消息级检测」层，并验证 force / forced_lang 仍优先（保留运营强制开关）。
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.integrations.line_rpa.runner import LineRpaRunner


def _runner(cfg, state_store=None):
    cm = SimpleNamespace(config_path=str(Path("config/config.yaml")))
    return LineRpaRunner(
        config_manager=cm, skill_manager=MagicMock(),
        line_rpa_cfg=cfg, state_store=state_store,
    )


class _StubStore:
    def __init__(self, state):
        self._state = state

    def get_chat_state(self, chat_key):
        return dict(self._state)


def test_follows_customer_language_when_no_force():
    r = _runner({"account_id": "a1", "default_reply_lang": "zh"})
    assert r._resolve_line_reply_lang("c1", "สวัสดีครับ อยากสอบถามราคาสินค้า") == "th"
    assert r._resolve_line_reply_lang("c1", "こんにちは、商品について質問があります") == "ja"
    assert r._resolve_line_reply_lang("c1", "안녕하세요 문의드립니다") == "ko"


def test_chinese_customer_still_chinese():
    r = _runner({"account_id": "a1", "default_reply_lang": "zh"})
    assert r._resolve_line_reply_lang("c1", "你好，我想咨询一下产品") == "zh"


def test_global_force_overrides_detection():
    r = _runner({"account_id": "a1", "force_reply_lang": "ja", "default_reply_lang": "zh"})
    # 即使客户说泰语，force=ja 仍强制日语（保留运营强制开关）
    assert r._resolve_line_reply_lang("c1", "สวัสดีครับ") == "ja"


def test_force_auto_does_not_override():
    r = _runner({"account_id": "a1", "force_reply_lang": "auto", "default_reply_lang": "zh"})
    # force=auto/detect 视为「不强制」，继续走检测
    assert r._resolve_line_reply_lang("c1", "Bonjour, je voudrais des informations") == "fr"


def test_per_chat_forced_lang_overrides_detection():
    store = _StubStore({"forced_lang": "en"})
    r = _runner({"account_id": "a1", "default_reply_lang": "zh"}, state_store=store)
    # per-chat 锁定 en，即使客户说泰语
    assert r._resolve_line_reply_lang("c1", "สวัสดีครับ") == "en"


def test_falls_back_to_last_peer_text_when_no_arg():
    store = _StubStore({"last_peer_text": "こんにちは、よろしくお願いします"})
    r = _runner({"account_id": "a1", "default_reply_lang": "zh"}, state_store=store)
    # 未传 peer_text → 回落 state_store 最近一条客户消息检测
    assert r._resolve_line_reply_lang("c1") == "ja"


def test_falls_back_to_default_when_undetectable():
    r = _runner({"account_id": "a1", "default_reply_lang": "zh"})
    # 空文本 + 无 state_store → default_reply_lang
    assert r._resolve_line_reply_lang("c1", "") == "zh"


def test_default_reply_lang_respected_on_empty():
    r = _runner({"account_id": "a1", "default_reply_lang": "ja"})
    assert r._resolve_line_reply_lang("c1", "   ") == "ja"
