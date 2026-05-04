from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock
import time

from src.integrations.messenger_rpa.chat_reader import PeerMessage
from src.integrations.messenger_rpa.runner import MessengerRpaRunner


def _runner() -> MessengerRpaRunner:
    return MessengerRpaRunner(
        config_manager=SimpleNamespace(config={}),
        skill_manager=MagicMock(),
        messenger_rpa_cfg={
            "thread_self_xml_guard": True,
            "thread_self_media_xml_guard": True,
        },
        state_store=MagicMock(),
    )


def _runner_with_state(
    cfg_overrides: dict | None = None,
    last_sent_at: float = 0.0,
    chat_name: str = "Victor Zan",
    screen_wh: tuple = (720, 1600),
) -> MessengerRpaRunner:
    """P1-C 测试用：runner 带可控 chat_state + screen_wh cache，
    避免 mock `_screen_size` 触发 adb 实际调用。"""
    state = MagicMock()
    state.get_chat_state = MagicMock(
        return_value={"last_sent_at": float(last_sent_at)},
    )
    cfg = {
        "thread_self_xml_guard": True,
        "thread_self_media_xml_guard": True,
        "thread_xml_bubble_guard": True,
        "thread_xml_bubble_guard_window_sec": 90,
        "thread_xml_bubble_guard_left_ratio": 0.6,
        "chat_key_prefix": "test",
    }
    cfg.update(cfg_overrides or {})
    r = MessengerRpaRunner(
        config_manager=SimpleNamespace(config={}),
        skill_manager=MagicMock(),
        messenger_rpa_cfg=cfg,
        state_store=state,
    )
    r._screen_wh_cache["dev1"] = screen_wh
    return r


def test_self_media_xml_guard_skips_vision_ocr_reply(monkeypatch) -> None:
    """A self-sent image icon must not become a peer text via screenshot OCR."""
    from src.integrations.messenger_rpa import thread_actions as ta

    xml = (
        "<hierarchy>"
        "<node class='android.widget.Button' "
        "content-desc='X, SimpleTextThreadSnippet(text=何してるの。)' "
        "bounds='[96,1040][620,1190]'/>"
        "<node class='android.widget.Button' "
        "content-desc='X, SimpleTextThreadSnippet(text=You: \U000f0000)' "
        "bounds='[0,1257][720,1394]'/>"
        "</hierarchy>"
    )
    monkeypatch.setattr(ta, "dump_view_tree", lambda *a, **kw: xml)

    result = {}
    peer_msg = PeerMessage(
        role="peer",
        kind="text",
        content="Not set",
        desc="",
        raw='{"role":"peer","kind":"text","content":"Not set","desc":""}',
    )

    assert _runner()._latest_thread_snippet_is_self(
        "dev1", result, peer_msg=peer_msg,
    ) is True
    assert result["thread_latest_has_self_prefix"] is True
    assert result["thread_latest_self_media"] is True
    assert "self_media_xml_guard" in result["hints"]


def test_self_media_xml_guard_waits_for_vision_before_skipping(monkeypatch) -> None:
    """The media-only self prefix is recorded but not decisive before Vision."""
    from src.integrations.messenger_rpa import thread_actions as ta

    xml = (
        "<hierarchy><node class='android.widget.Button' "
        "content-desc='X, SimpleTextThreadSnippet(text=You: \U000f0000)' "
        "bounds='[0,1257][720,1394]'/></hierarchy>"
    )
    monkeypatch.setattr(ta, "dump_view_tree", lambda *a, **kw: xml)

    result = {}
    assert _runner()._latest_thread_snippet_is_self("dev1", result) is False
    assert result["thread_latest_self_media"] is True


def test_self_media_xml_guard_does_not_skip_natural_peer_text(monkeypatch) -> None:
    from src.integrations.messenger_rpa import thread_actions as ta

    xml = (
        "<hierarchy>"
        "<node class='android.widget.Button' "
        "content-desc='X, SimpleTextThreadSnippet(text=You: \U000f0000)' "
        "bounds='[0,1257][720,1394]'/>"
        "</hierarchy>"
    )
    monkeypatch.setattr(ta, "dump_view_tree", lambda *a, **kw: xml)

    result = {}
    peer_msg = PeerMessage(
        role="peer",
        kind="text",
        content="これで、欲しいものが手に入る。",
        desc="",
        raw='{"role":"peer","kind":"text","content":"これで、欲しいものが手に入る。","desc":""}',
    )

    assert _runner()._latest_thread_snippet_is_self(
        "dev1", result, peer_msg=peer_msg,
    ) is False
    assert result["thread_latest_self_media"] is True
    assert "self_media_xml_guard_ignored_natural_peer" in result["hints"]


def test_promote_extra_peer_after_self_overlap() -> None:
    result = {
        "extra_peers": [
            {
                "kind": "text",
                "content": "これで、欲しいものが手に入る。",
                "desc": "表达愿望",
            }
        ]
    }
    promoted = _runner()._promote_extra_peer_after_self_overlap(
        result,
        last_reply="こんばんは、佐藤です。今日はどんな一日でしたか？",
    )
    assert promoted is not None
    assert promoted.kind == "text"
    assert "欲しいもの" in promoted.content


def test_promote_extra_peer_ignores_self_reply_overlap() -> None:
    result = {
        "extra_peers": [
            {
                "kind": "text",
                "content": "こんばんは、佐藤です。今日はどんな一日でしたか？",
                "desc": "",
            }
        ]
    }
    promoted = _runner()._promote_extra_peer_after_self_overlap(
        result,
        last_reply="こんばんは、佐藤です。今日はどんな一日でしたか？",
    )
    assert promoted is None


def test_detects_send_to_share_picker() -> None:
    xml = (
        "<hierarchy>"
        "<node class='android.widget.TextView' text='Send to' bounds='[0,80][300,160]'/>"
        "<node class='android.widget.TextView' text='CREATE GROUP' bounds='[480,80][700,160]'/>"
        "<node class='android.widget.EditText' text='Write a message...' bounds='[40,390][600,450]'/>"
        "<node class='android.widget.Button' text='Send' bounds='[540,600][688,670]'/>"
        "</hierarchy>"
    )
    assert _runner()._is_send_to_screen_xml(xml) is True


def test_does_not_treat_normal_inbox_as_send_to() -> None:
    xml = (
        "<hierarchy>"
        "<node class='android.widget.TextView' text='messenger' bounds='[0,80][320,160]'/>"
        "<node class='android.widget.EditText' text='Ask Meta AI or Search' bounds='[32,190][688,270]'/>"
        "<node class='android.widget.Button' content-desc='Victor Zan, SimpleTextThreadSnippet(text=hi)' bounds='[0,600][720,740]'/>"
        "</hierarchy>"
    )
    assert _runner()._is_send_to_screen_xml(xml) is False


def test_search_result_fallback_taps_use_search_layout() -> None:
    taps = _runner()._search_result_fallback_taps((720, 1600))
    assert taps[0] == (280, 350, "search_result_primary")
    assert taps[0][1] < 500


def test_current_thread_fast_path_requires_target_allowlist() -> None:
    result = {}
    assert _runner()._current_thread_target_from_title(
        "Victor Zan", [], result,
    ) is None
    assert result["current_thread_title"] == "Victor Zan"
    assert "current_thread_seen:no_target_allowlist" in result["hints"]


def test_current_thread_fast_path_builds_synthetic_target() -> None:
    result = {}
    target = _runner()._current_thread_target_from_title(
        "Victor Zan", ["Victor Zan"], result,
    )

    assert target is not None
    assert target.name == "Victor Zan"
    assert target.skip_inbox_tap is True
    assert target.quality_hint == "current_thread"


def test_current_thread_fast_path_rejects_wrong_target() -> None:
    result = {}
    assert _runner()._current_thread_target_from_title(
        "Someone Else", ["Victor Zan"], result,
    ) is None
    assert "current_thread_seen:not_target:Someone Else" in result["hints"]


def test_run_once_start_mode_defaults_to_smart_current_thread() -> None:
    assert _runner()._run_once_start_mode() == "smart_current_thread"


def test_run_once_start_mode_can_force_chats() -> None:
    r = _runner()
    r._cfg["run_once_start_mode"] = "force_chats"
    assert r._run_once_start_mode() == "force_chats"


def test_run_once_start_mode_force_return_to_chats_wins() -> None:
    r = _runner()
    r._cfg["run_once_start_mode"] = "smart_current_thread"
    r._cfg["force_return_to_chats"] = True
    assert r._run_once_start_mode() == "force_chats"


def test_refresh_cfg_updates_reply_mode_cache() -> None:
    r = _runner()
    r._reply_mode = "auto"
    r.refresh_cfg({"reply_mode": "off"})
    assert r._reply_mode == "off"


def test_stale_peer_after_recent_self_marker_detects_repeated_peer() -> None:
    r = _runner()
    result = {
        "thread_latest_has_self_prefix": True,
        "thread_latest_self_media": True,
    }
    chat_state = {
        "last_sent_at": time.time(),
        "last_peer_text": "今日はどんな一日でしたか？\nネットは繋がっていますか？",
    }
    peer = PeerMessage(
        role="peer",
        kind="text",
        content="今日はどんな一日でしたか？\nフィーはどうですか？",
        desc="",
        raw="{}",
    )

    assert r._stale_peer_after_recent_self_marker(chat_state, peer, result) is True
    assert result["last_peer_repeat_overlap"] >= 0.45


def test_stale_peer_after_recent_self_marker_allows_new_text() -> None:
    r = _runner()
    result = {
        "thread_latest_has_self_prefix": True,
        "thread_latest_self_media": True,
    }
    chat_state = {
        "last_sent_at": time.time(),
        "last_peer_text": "今日はどんな一日でしたか？",
    }
    peer = PeerMessage(
        role="peer",
        kind="text",
        content="仕事はエンジニアです。横浜に住んでいます。",
        desc="",
        raw="{}",
    )

    assert r._stale_peer_after_recent_self_marker(chat_state, peer, result) is False


# ════════════════════════════════════════════════════════════════════
#  P1-C: thread 内气泡 cx 几何 + last_sent_at 时间窗双信号守卫
#  （死循环复现：thread 内自方蓝色气泡被 vision hallucinate 为 peer）
# ════════════════════════════════════════════════════════════════════


def _thread_xml_with_self_bubble(text: str = '今日はまだ食べてないよ') -> str:
    """模拟 thread 内自方蓝色气泡（cx 在右侧）。
    bounds=[400,1100][700,1200] → cx=550，screen_w=720 → 550/720=0.76 > 0.6 阈值。"""
    return (
        "<hierarchy>"
        "<node class='android.widget.FrameLayout' "
        "resource-id='com.facebook.orca:id/thread_view_content' "
        "bounds='[0,200][720,1400]'/>"
        f"<node class='android.view.ViewGroup' text='{text}' "
        "bounds='[400,1100][700,1200]'/>"
        "</hierarchy>"
    )


def _thread_xml_with_peer_bubble(text: str = '対方が言った何か') -> str:
    """模拟 thread 内对方灰色气泡（cx 在左侧）。
    bounds=[40,1100][320,1200] → cx=180，180/720=0.25 < 0.6。"""
    return (
        "<hierarchy>"
        "<node class='android.widget.FrameLayout' "
        "resource-id='com.facebook.orca:id/thread_view_content' "
        "bounds='[0,200][720,1400]'/>"
        f"<node class='android.view.ViewGroup' text='{text}' "
        "bounds='[40,1100][320,1200]'/>"
        "</hierarchy>"
    )


class TestP1cThreadXmlBubbleGuard:
    """P1-C: 死循环 reproduction — thread 内自方蓝色气泡 + 我方刚发不久。"""

    def test_p1c_self_bubble_within_window_blocks(self, monkeypatch):
        """我方 30s 前刚发 + thread XML 显示气泡在右侧 → P1-C 拦下，不调 vision。"""
        from src.integrations.messenger_rpa import thread_actions as ta
        monkeypatch.setattr(
            ta, 'dump_view_tree',
            lambda *a, **kw: _thread_xml_with_self_bubble(),
        )
        r = _runner_with_state(last_sent_at=time.time() - 30)
        result = {'chat_name': 'Victor Zan'}
        assert r._latest_thread_snippet_is_self('dev1', result) is True
        assert 'thread_xml_bubble_guard:self' in result.get('hints', [])
        assert result['thread_xml_bubble_dbg'].startswith('self ')

    def test_p1c_self_bubble_outside_window_passes(self, monkeypatch):
        """我方 200s 前发过（超出 90s 窗口）→ 守卫不参与，让 vision 处理。
        旧 latest_snippet_row 也找不到 SimpleTextThreadSnippet → 整体 pass。"""
        from src.integrations.messenger_rpa import thread_actions as ta
        monkeypatch.setattr(
            ta, 'dump_view_tree',
            lambda *a, **kw: _thread_xml_with_self_bubble(),
        )
        r = _runner_with_state(last_sent_at=time.time() - 200)
        result = {'chat_name': 'Victor Zan'}
        assert r._latest_thread_snippet_is_self('dev1', result) is False

    def test_p1c_peer_bubble_does_not_trip(self, monkeypatch):
        """thread 内最末气泡是 peer 左侧 → 即使我方刚发也不该拦下。"""
        from src.integrations.messenger_rpa import thread_actions as ta
        monkeypatch.setattr(
            ta, 'dump_view_tree',
            lambda *a, **kw: _thread_xml_with_peer_bubble(),
        )
        r = _runner_with_state(last_sent_at=time.time() - 30)
        result = {'chat_name': 'Victor Zan'}
        assert r._latest_thread_snippet_is_self('dev1', result) is False
        assert result['thread_xml_bubble_dbg'].startswith('peer ')

    def test_p1c_disabled_by_config(self, monkeypatch):
        """thread_xml_bubble_guard=false → P1-C 完全关闭。"""
        from src.integrations.messenger_rpa import thread_actions as ta
        monkeypatch.setattr(
            ta, 'dump_view_tree',
            lambda *a, **kw: _thread_xml_with_self_bubble(),
        )
        r = _runner_with_state(
            cfg_overrides={'thread_xml_bubble_guard': False},
            last_sent_at=time.time() - 30,
        )
        result = {'chat_name': 'Victor Zan'}
        # 旧路径（latest_snippet_row）找不到 SimpleTextThreadSnippet → False
        assert r._latest_thread_snippet_is_self('dev1', result) is False

    def test_p1c_no_chat_name_skips_safely(self, monkeypatch):
        """result 没 chat_name + 调用方没传 chat_name → P1-C 跳过（不拦），避免误判。"""
        from src.integrations.messenger_rpa import thread_actions as ta
        monkeypatch.setattr(
            ta, 'dump_view_tree',
            lambda *a, **kw: _thread_xml_with_self_bubble(),
        )
        r = _runner_with_state(last_sent_at=time.time() - 30)
        result = {}  # 没 chat_name
        # 没 chat_name → 不读 last_sent_at → 不进入 P1-C 分支
        assert r._latest_thread_snippet_is_self('dev1', result) is False

    def test_p1c_chat_name_explicit_arg_overrides_result(self, monkeypatch):
        """显式传 chat_name 参数（不依赖 result['chat_name']）也能命中。"""
        from src.integrations.messenger_rpa import thread_actions as ta
        monkeypatch.setattr(
            ta, 'dump_view_tree',
            lambda *a, **kw: _thread_xml_with_self_bubble(),
        )
        r = _runner_with_state(last_sent_at=time.time() - 30)
        result = {}
        assert r._latest_thread_snippet_is_self(
            'dev1', result, chat_name='Victor Zan',
        ) is True

    def test_p1c_left_ratio_configurable(self, monkeypatch):
        """left_ratio 可配置——提到 0.85 后，cx=550 (550/720=0.76) 不再算 self。"""
        from src.integrations.messenger_rpa import thread_actions as ta
        monkeypatch.setattr(
            ta, 'dump_view_tree',
            lambda *a, **kw: _thread_xml_with_self_bubble(),
        )
        r = _runner_with_state(
            cfg_overrides={'thread_xml_bubble_guard_left_ratio': 0.85},
            last_sent_at=time.time() - 30,
        )
        result = {'chat_name': 'Victor Zan'}
        assert r._latest_thread_snippet_is_self('dev1', result) is False

    def test_p1c_dump_view_tree_failure_falls_through(self, monkeypatch):
        """dump_view_tree 返回 None → 走原 'no_xml' 分支，不抛异常。"""
        from src.integrations.messenger_rpa import thread_actions as ta
        monkeypatch.setattr(ta, 'dump_view_tree', lambda *a, **kw: None)
        r = _runner_with_state(last_sent_at=time.time() - 30)
        result = {'chat_name': 'Victor Zan'}
        assert r._latest_thread_snippet_is_self('dev1', result) is False
        assert 'thread_self_xml_guard:no_xml' in result.get('hints', [])

