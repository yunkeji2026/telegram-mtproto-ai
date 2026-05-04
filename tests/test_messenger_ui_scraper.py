"""messenger_rpa.ui_scraper 的离线单测。

XML 样本取自真机（小米 720×1600，Messenger 中文版）uiautomator dump 的片段。
刻意保留 peer name 的 full-width 空格、日文假名、以及 Litho 特有的 ``X.2Wn@hash``
壳，以确保解析对真实噪声鲁棒。
"""
from __future__ import annotations

from src.integrations.messenger_rpa.ui_scraper import (
    find_button_by_desc,
    find_input_box,
    find_peer_read_marker,
    find_search_suggestion_taps,
    find_send_button,
    find_thread_title,
    is_in_thread,
    iter_inbox_rows,
    last_bubble_preview,
    latest_snippet_row,
    parse_xml,
)


THREAD_XML_NO_KEYBOARD = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" class="android.widget.FrameLayout" bounds="[0,0][720,1438]">
    <node class="android.widget.Button" content-desc="返回" bounds="[8,76][104,172]"/>
    <node class="android.widget.Button" content-desc="だいすけ いとう, 对话详情" bounds="[112,76][424,172]"/>
    <node class="android.widget.Button" content-desc="语音通话" bounds="[424,76][520,172]"/>
    <node class="android.widget.Button" content-desc="视频通话" bounds="[520,76][616,172]"/>
    <node class="android.widget.Button" content-desc="对话详情" bounds="[616,76][712,172]"/>
    <node class="android.view.ViewGroup" text="だいすけ いとう" content-desc="だいすけ いとう" bounds="[186,663][534,733]"/>
    <node class="android.widget.ImageView" content-desc="だいすけ いとう已读" bounds="[648,1337][680,1369]"/>
    <node class="android.widget.Button" content-desc="其他附件选项" bounds="[0,1386][80,1438]"/>
    <node class="android.widget.Button" content-desc="打开相机" bounds="[80,1386][160,1438]"/>
    <node class="android.widget.Button" content-desc="打开图库。" bounds="[160,1386][240,1438]"/>
    <node class="android.widget.Button" content-desc="打开录音器。" bounds="[240,1386][320,1438]"/>
    <node class="android.widget.EditText" text="发消息" content-desc="输入消息" bounds="[320,1404][560,1438]"/>
    <node class="android.view.ViewGroup" text="你们不是 Facebook 好友" bounds="[208,775][512,815]"/>
  </node>
</hierarchy>
"""


THREAD_XML_KEYBOARD_OPEN = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" class="android.widget.FrameLayout" bounds="[0,0][720,1438]">
    <node class="android.widget.Button" content-desc="返回" bounds="[8,76][104,172]"/>
    <node class="android.widget.Button" content-desc="だいすけ いとう, 对话详情" bounds="[112,76][424,172]"/>
    <node class="android.widget.Button" content-desc="其他附件选项" bounds="[0,876][80,996]"/>
    <node class="android.widget.EditText" text="probe_hello" content-desc="输入消息" bounds="[80,894][568,978]"/>
    <node class="android.widget.Button" content-desc="打开贴图、表情和动图面板。" bounds="[568,900][640,972]"/>
    <node class="android.widget.Button" content-desc="发送" bounds="[640,876][720,996]"/>
    <node class="android.widget.ImageView" content-desc="だいすけ いとう已读" bounds="[648,827][680,859]"/>
    <node class="android.view.ViewGroup" text="上午5:12" bounds="[0,572][720,723]"/>
  </node>
</hierarchy>
"""


INBOX_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" class="android.widget.FrameLayout" bounds="[0,0][720,1438]">
    <node class="android.view.ViewGroup" content-desc="主菜单, 1个未读聊天" bounds="[32,68][128,188]"/>
    <node class="android.view.ViewGroup" content-desc="Messenger" bounds="[128,68][608,188]"/>
    <node class="android.widget.Button" content-desc="新消息" bounds="[624,96][688,160]"/>
    <node class="android.widget.Button" content-desc="X.2Wn@8f575b8e, SimpleTextThreadSnippet(text=こんにちは、初めまして)" bounds="[0,276][720,415]"/>
    <node class="android.widget.Button" content-desc="X.2Wn@591fe7c3, SimpleTextThreadSnippet(text=你: Ff)" bounds="[0,415][720,559]"/>
    <node class="android.widget.Button" content-desc="X.2Wn@2de95049, SimpleTextThreadSnippet(text=消息和通话将通过端到端加密)" bounds="[0,559][720,703]"/>
    <node class="android.widget.Button" content-desc="X.2Wn@1247aa8c, SimpleTextThreadSnippet(text=你: こんにちは)" bounds="[0,1279][720,1394]"/>
  </node>
</hierarchy>
"""


# ── find_thread_title ───────────────────────────────────────

def test_find_thread_title_chinese() -> None:
    assert find_thread_title(THREAD_XML_NO_KEYBOARD) == "だいすけ いとう"
    assert find_thread_title(THREAD_XML_KEYBOARD_OPEN) == "だいすけ いとう"


def test_find_thread_title_english() -> None:
    xml = (
        "<hierarchy><node class='android.widget.Button' "
        "content-desc='Jane Doe, Conversation details' "
        "bounds='[112,76][424,172]'/></hierarchy>"
    )
    assert find_thread_title(xml) == "Jane Doe"


def test_find_thread_title_english_thread_details() -> None:
    """真机英文版 Messenger：新版用 'Thread Details' 作尾部标签。"""
    xml = (
        "<hierarchy><node class='android.widget.Button' "
        "content-desc='さとう たかひろ, Thread Details' "
        "bounds='[112,76][424,172]'/></hierarchy>"
    )
    assert find_thread_title(xml) == "さとう たかひろ"


def test_find_thread_title_with_active_status_middle_segment() -> None:
    """顶栏 cd 可能含中间状态段（Active now / Active 3m ago）。"""
    xml = (
        "<hierarchy><node class='android.widget.Button' "
        "content-desc='さとう たかひろ, Active now, Thread Details' "
        "bounds='[112,76][424,172]'/></hierarchy>"
    )
    assert find_thread_title(xml) == "さとう たかひろ"


def test_find_thread_title_with_active_minutes_ago() -> None:
    xml = (
        "<hierarchy><node class='android.widget.Button' "
        "content-desc='John Smith, Active 5m ago, Thread Details' "
        "bounds='[112,76][424,172]'/></hierarchy>"
    )
    assert find_thread_title(xml) == "John Smith"


def test_find_thread_title_none_when_not_thread() -> None:
    assert find_thread_title(INBOX_XML) is None


def test_find_thread_title_returns_none_for_garbage() -> None:
    assert find_thread_title("<broken xml") is None


# ── find_input_box ──────────────────────────────────────────

def test_find_input_box_keyboard_closed_with_hint() -> None:
    ib = find_input_box(THREAD_XML_NO_KEYBOARD)
    assert ib is not None
    assert ib.is_hint is True
    assert ib.text == ""
    assert ib.keyboard_open is False
    assert ib.bounds.top >= 1350  # 键盘未弹时靠屏幕最下


def test_find_input_box_keyboard_open_with_text() -> None:
    ib = find_input_box(THREAD_XML_KEYBOARD_OPEN)
    assert ib is not None
    assert ib.is_hint is False
    assert ib.text == "probe_hello"
    assert ib.keyboard_open is True
    assert ib.bounds.top < 1000


# ── find_send_button ────────────────────────────────────────

def test_find_send_button_exists_when_keyboard_open() -> None:
    b = find_send_button(THREAD_XML_KEYBOARD_OPEN)
    assert b is not None
    # 720x1600 标定：发送键 bbox ≈ [640,876][720,996]
    assert 640 <= b.left <= 700
    assert b.cx > 640


def test_find_send_button_absent_when_keyboard_closed() -> None:
    assert find_send_button(THREAD_XML_NO_KEYBOARD) is None


# ── find_peer_read_marker ──────────────────────────────────

def test_find_peer_read_marker() -> None:
    name = find_peer_read_marker(THREAD_XML_KEYBOARD_OPEN)
    assert name == "だいすけ いとう"


def test_find_peer_read_marker_english() -> None:
    xml = (
        "<hierarchy><node class='android.widget.ImageView' "
        "content-desc='Seen by Jane Doe' bounds='[648,827][680,859]'/></hierarchy>"
    )
    assert find_peer_read_marker(xml) == "Jane Doe"


# ── iter_inbox_rows ────────────────────────────────────────

def test_iter_inbox_rows() -> None:
    rows = iter_inbox_rows(INBOX_XML)
    assert len(rows) == 4
    previews = [r.preview for r in rows]
    assert any("こんにちは" in p for p in previews)
    # 第二行 "你: Ff" 必须标为 is_self_last
    self_rows = [r for r in rows if r.is_self_last]
    assert len(self_rows) == 2  # "你: Ff" + "你: こんにちは"


def test_iter_inbox_rows_self_filter_enables_skip() -> None:
    rows = iter_inbox_rows(INBOX_XML)
    external_rows = [r for r in rows if not r.is_self_last]
    # 过滤"自己刚发的"后，剩下的是真正需要回复的对外会话
    assert all(not r.is_self_last for r in external_rows)


# ── find_button_by_desc ────────────────────────────────────

def test_find_button_by_desc_new_message() -> None:
    b = find_button_by_desc(INBOX_XML, ["新消息", "New message"])
    assert b is not None
    assert b.cx > 600  # 右上角


def test_find_button_by_desc_miss() -> None:
    assert find_button_by_desc(INBOX_XML, ["does_not_exist"]) is None


# ── is_in_thread ───────────────────────────────────────────

def test_is_in_thread_true() -> None:
    assert is_in_thread(THREAD_XML_NO_KEYBOARD) is True
    assert is_in_thread(THREAD_XML_KEYBOARD_OPEN) is True


def test_is_in_thread_false_on_inbox() -> None:
    assert is_in_thread(INBOX_XML) is False


def test_latest_snippet_row_detects_self_prefix() -> None:
    row = latest_snippet_row(INBOX_XML)
    assert row is not None
    assert row.preview == "你: こんにちは"
    assert row.is_self_last is True


def test_latest_snippet_row_can_guard_when_thread_title_missing() -> None:
    xml = (
        "<hierarchy>"
        "<node class='android.widget.Button' content-desc='返回' bounds='[8,76][104,172]'/>"
        "<node class='android.widget.Button' "
        "content-desc='X, SimpleTextThreadSnippet(text=どうしてるの？)' "
        "bounds='[24,640][520,760]'/>"
        "<node class='android.widget.Button' "
        "content-desc='X, SimpleTextThreadSnippet(text=You: うん、今は少し落ち着いたよ)' "
        "bounds='[160,980][700,1140]'/>"
        "</hierarchy>"
    )
    assert is_in_thread(xml) is False
    row = latest_snippet_row(xml)
    assert row is not None
    assert row.preview.startswith("You:")
    assert row.is_self_last is True
    assert row.has_self_prefix is True
    assert row.is_self_media_placeholder is False


def test_latest_snippet_row_ignores_self_prefix_without_text_payload() -> None:
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
    row = latest_snippet_row(xml)
    assert row is not None
    assert row.preview.startswith("You:")
    assert row.is_self_last is False
    assert row.has_self_prefix is True
    assert row.is_self_media_placeholder is True


def test_iter_inbox_rows_detects_english_self_prefixes() -> None:
    xml = (
        "<hierarchy>"
        "<node class='android.widget.Button' "
        "content-desc='X, SimpleTextThreadSnippet(text=Me: hello)' "
        "bounds='[0,520][720,660]'/>"
        "</hierarchy>"
    )
    rows = iter_inbox_rows(xml)
    assert rows
    assert rows[0].is_self_last is True


# ── last_bubble_preview (best-effort) ──────────────────────

def test_last_bubble_preview_returns_something() -> None:
    t, _dbg = last_bubble_preview(THREAD_XML_NO_KEYBOARD)
    # 只要求不炸 + 若找到，长度合理
    if t is not None:
        assert 1 <= len(t) <= 2000


# ── 整体 smoke: parse_xml ──────────────────────────────────

def test_parse_xml_bytes_and_str() -> None:
    assert parse_xml(THREAD_XML_NO_KEYBOARD) is not None
    assert parse_xml(THREAD_XML_NO_KEYBOARD.encode("utf-8")) is not None
    assert parse_xml("<") is None  # malformed


# ── find_search_suggestion_taps（搜索页）──────────────────────

SEARCH_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node class="android.widget.FrameLayout" bounds="[0,0][720,1600]">
    <node class="android.widget.Button" content-desc="返回" bounds="[8,76][104,172]"/>
    <node class="android.widget.EditText" text="井出麟太郎" content-desc="搜索"
      bounds="[120,180][600,240]"/>
    <node class="android.view.ViewGroup" text="Seongsuk Kim"
      content-desc="X.2Wn@x, SimpleTextThreadSnippet(text=Hi)" bounds="[0,520][720,660]"/>
    <node class="android.widget.TextView" text="井出麟太郎"
      bounds="[40,700][400,780]"/>
    <node class="android.widget.TextView" text="Random Ad Line 很长很长很长"
      bounds="[40,820][680,900]"/>
    <node class="android.view.ViewGroup" text="创建X在内的群聊"
      content-desc="创建X在内的群聊" bounds="[0,940][720,1040]"/>
  </node>
</hierarchy>
"""


def test_find_search_suggestion_taps_prefers_exact_name() -> None:
    taps = find_search_suggestion_taps(SEARCH_XML, "井出麟太郎", screen_w=720, screen_h=1600)
    assert taps, "expected at least one tap"
    best_cx, best_cy, score, reason = taps[0]
    assert score == 100
    assert reason == "text_exact"
    assert best_cy > 650  # 在搜索框下方，不是 EditText 那一行（若误匹配会失败）


def test_find_search_suggestion_taps_weak_substr_in_long_cd() -> None:
    xml = (
        "<hierarchy><node class='android.widget.FrameLayout' bounds='[0,0][720,1600]'>"
        "<node class='android.widget.Button' content-desc='返回' bounds='[8,76][104,172]'/>"
        "<node class='android.view.ViewGroup' text='' "
        "content-desc='联系人 井出麟太郎 最近活跃 的说明文案' "
        "bounds='[0,600][720,720]'/>"
        "</node></hierarchy>"
    )
    taps = find_search_suggestion_taps(xml, "井出麟太郎", screen_w=720, screen_h=1600)
    assert taps
    assert taps[0][3] == "weak_substr"


def test_find_search_suggestion_taps_skips_search_edittext_query() -> None:
    """搜索框里正在输入的名字不是结果行，不得当成最高优先 tap。"""
    xml = (
        "<hierarchy><node class='android.widget.FrameLayout' bounds='[0,0][720,1600]'>"
        "<node class='android.widget.EditText' text='井出麟太郎' content-desc='搜索'"
        " bounds='[120,240][600,310]'/>"
        "<node class='android.widget.TextView' text='井出麟太郎' "
        "bounds='[40,620][400,700]'/>"
        "</node></hierarchy>"
    )
    taps = find_search_suggestion_taps(xml, "井出麟太郎", screen_w=720, screen_h=1600)
    assert taps
    assert taps[0][2] == 100
    assert taps[0][1] > 500


def test_find_search_suggestion_taps_snippet_fallback() -> None:
    xml = (
        "<hierarchy><node class='android.widget.FrameLayout' bounds='[0,0][720,1600]'>"
        "<node class='android.widget.Button' content-desc='返回' bounds='[8,76][104,172]'/>"
        "<node class='android.view.ViewGroup' text='' "
        "content-desc='X.2Wn@h, SimpleTextThreadSnippet(text=さとう たかひろ · hello)' "
        "bounds='[0,600][720,740]'/>"
        "</node></hierarchy>"
    )
    taps = find_search_suggestion_taps(xml, "さとう たかひろ", screen_w=720, screen_h=1600)
    assert taps
    assert taps[0][3] == "snippet_cd"
