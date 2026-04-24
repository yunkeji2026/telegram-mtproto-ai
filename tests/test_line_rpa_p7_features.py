"""P7：LINE 多模态系统提示 + 告警按 kind 计数 + Skill 上下文合并。"""

from __future__ import annotations

from src.integrations.line_rpa.state_store import LineRpaStateStore
from src.ai.ai_client import AIClient


def test_alerts_count_unacked_by_kind(tmp_path):
    store = LineRpaStateStore(tmp_path / "t.db")
    store.insert_alert(kind="ime_lost", message="a", dedup_window_sec=0)
    store.insert_alert(kind="adb_lost", message="b", dedup_window_sec=0)
    assert store.alerts_count_unacked() == 2
    assert store.alerts_count_unacked(kind="ime_lost") == 1
    assert store.alerts_count_unacked(kind="adb_lost") == 1


class _Cfg:
    config_path = None
    config = {"web_admin": {"site_name": "T"}, "ai": {}}

    def get_ai_config(self):
        return {}


def test_ai_context_prompt_line_sticker_hint():
    client = AIClient(_Cfg())
    ctx = {
        "channel": "line_rpa",
        "_current_user_message_for_lang": "[LINE贴图] 棕熊",
        "line_rpa_style_hint": "",
    }
    out = client._build_context_prompt(ctx)
    assert "多模态·贴图" in out


def test_ai_context_prompt_vision_room_flag():
    client = AIClient(_Cfg())
    ctx = {
        "channel": "line_rpa",
        "_current_user_message_for_lang": "hello",
        "vision_room": True,
    }
    out = client._build_context_prompt(ctx)
    assert "读屏模式" in out
