"""LINE RPA 多会话能力最小测试：

- screen_state.detect_screen_state：伪 XML 覆盖 ChatRoom / ChatList / OtherLine / Lock / OtherApp
- chat_list_scanner.parse_unread_rows：未读数字徽章识别 + 同行匹配姓名 + 多行
- ui_hierarchy.find_topbar_title / has_back_button
- runner.LineRpaRunner._row_allowed：白/黑名单策略
"""

from __future__ import annotations

import pytest

from src.integrations.line_rpa import screen_state as ss
from src.integrations.line_rpa import ui_hierarchy as ui
from src.integrations.line_rpa.chat_list_scanner import UnreadRow, parse_unread_rows
from src.integrations.line_rpa.runner import LineRpaRunner


# ───── 小工具：构造 uiautomator 风格 XML ─────

def _hier(pkg: str, nodes: str) -> bytes:
    """组装一个最小可解析的 hierarchy XML（bytes）。"""
    return (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>\n"
        "<hierarchy rotation='0'>\n"
        f"  <node index='0' bounds='[0,0][1080,2340]' package='{pkg}' "
        "class='android.widget.FrameLayout'>\n"
        f"{nodes}\n"
        "  </node>\n"
        "</hierarchy>\n"
    ).encode("utf-8")


def _node_textview(
    text: str,
    bounds: str,
    rid: str = "",
    pkg: str = "jp.naver.line.android",
    cdesc: str = "",
) -> str:
    rid_attr = f"resource-id='{rid}'" if rid else "resource-id=''"
    cdesc_attr = f"content-desc='{cdesc}'" if cdesc else "content-desc=''"
    return (
        f"    <node class='android.widget.TextView' text='{text}' "
        f"bounds='{bounds}' package='{pkg}' {rid_attr} {cdesc_attr}/>"
    )


def _node_edittext(bounds: str, pkg: str = "jp.naver.line.android") -> str:
    return (
        f"    <node class='android.widget.EditText' text='' "
        f"bounds='{bounds}' package='{pkg}' resource-id='{pkg}:id/message_edit' "
        "content-desc=''/>"
    )


def _node_button(
    bounds: str,
    pkg: str = "jp.naver.line.android",
    rid_tail: str = "chat_send",
    cdesc: str = "Send",
) -> str:
    return (
        f"    <node class='android.widget.ImageView' text='' bounds='{bounds}' "
        f"package='{pkg}' resource-id='{pkg}:id/{rid_tail}' "
        f"content-desc='{cdesc}'/>"
    )


# ───── screen_state ─────

def test_detect_chat_room():
    xml = _hier(
        "jp.naver.line.android",
        "\n".join([
            _node_textview("Alice", "[100,100][500,180]", rid="jp.naver.line.android:id/header_title"),
            _node_edittext("[40,2100][800,2220]"),
            _node_button("[820,2100][1020,2220]", rid_tail="chat_send", cdesc="Send"),
            _node_textview("Back", "[0,50][100,150]", cdesc="Navigate up"),
        ]),
    )
    state, reason = ss.detect_screen_state(xml)
    assert state == ss.CHAT_ROOM, reason


def test_detect_chat_list():
    xml = _hier(
        "jp.naver.line.android",
        "\n".join([
            _node_textview(
                "Alice", "[200,300][600,400]",
                rid="jp.naver.line.android:id/chatlist_row_name",
            ),
            _node_textview(
                "Bob", "[200,520][600,620]",
                rid="jp.naver.line.android:id/chatlist_row_name",
            ),
            _node_textview(
                "2", "[950,320][1030,400]",
                rid="jp.naver.line.android:id/chatlist_row_unread_count",
            ),
        ]),
    )
    state, reason = ss.detect_screen_state(xml)
    assert state == ss.CHAT_LIST, reason


def test_detect_other_line():
    xml = _hier(
        "jp.naver.line.android",
        _node_textview(
            "VOOM", "[100,200][400,300]",
            rid="jp.naver.line.android:id/voom_tab_title",
        ),
    )
    state, _ = ss.detect_screen_state(xml)
    assert state == ss.OTHER_LINE


def test_detect_other_app():
    xml = _hier(
        "com.foo.other",
        _node_textview("Hello", "[0,0][200,200]", pkg="com.foo.other"),
    )
    state, _ = ss.detect_screen_state(xml)
    assert state == ss.OTHER_APP


def test_detect_lock_screen():
    xml = _hier(
        "com.android.systemui",
        _node_textview("09:41", "[0,0][200,200]", pkg="com.android.systemui"),
    )
    state, _ = ss.detect_screen_state(xml)
    assert state == ss.LOCK_SCREEN


def test_detect_no_xml():
    state, _ = ss.detect_screen_state(None)
    assert state == ss.UNKNOWN
    state2, _ = ss.detect_screen_state(b"<not-xml>")
    assert state2 == ss.UNKNOWN


# ───── chat_list_scanner ─────

def _chat_list_xml_with_unread(names_counts):
    """
    names_counts: [(name, unread, y_top), ...]
      单行高 140px，badge 位于右侧
    """
    rows_xml = []
    for name, count, y in names_counts:
        rows_xml.append(_node_textview(
            name, f"[200,{y+20}][700,{y+100}]",
            rid="jp.naver.line.android:id/chatlist_row_name",
        ))
        rows_xml.append(_node_textview(
            str(count), f"[950,{y+30}][1020,{y+90}]",
            rid="jp.naver.line.android:id/chatlist_row_unread_count",
        ))
        # 时间戳（右上），应不被当成 badge
        rows_xml.append(_node_textview(
            "10:24", f"[800,{y+10}][930,{y+60}]",
            rid="jp.naver.line.android:id/chatlist_row_time",
        ))
    return _hier("jp.naver.line.android", "\n".join(rows_xml))


def test_parse_unread_rows_basic():
    xml = _chat_list_xml_with_unread([
        ("Alice", 2, 300),
        ("Bob", 1, 600),
        ("Carol", 12, 900),
    ])
    rows, dbg = parse_unread_rows(xml)
    assert len(rows) == 3, dbg
    names = [r.name for r in rows]
    assert names == ["Alice", "Bob", "Carol"]
    assert rows[0].unread_count == 2
    assert rows[2].unread_count == 12
    # tap y 应在姓名同一行附近
    assert 300 < rows[0].tap_y < 450


def test_parse_unread_rows_ignores_timestamp_only():
    # 只有时间戳没有 badge：应识别为 0 行未读
    xml = _hier(
        "jp.naver.line.android",
        "\n".join([
            _node_textview(
                "Alice", "[200,320][600,400]",
                rid="jp.naver.line.android:id/chatlist_row_name",
            ),
            _node_textview(
                "10:24", "[800,320][930,380]",
                rid="jp.naver.line.android:id/chatlist_row_time",
            ),
        ]),
    )
    rows, _ = parse_unread_rows(xml)
    assert rows == []


def test_parse_unread_rows_dedupes_near_rows():
    # 同一行出现两个徽章节点（resource-id 不同），应只识别为一行
    xml = _hier(
        "jp.naver.line.android",
        "\n".join([
            _node_textview(
                "Alice", "[200,320][600,400]",
                rid="jp.naver.line.android:id/chatlist_row_name",
            ),
            _node_textview(
                "3", "[950,330][1010,390]",
                rid="jp.naver.line.android:id/chatlist_row_unread_count",
            ),
            _node_textview(
                "3", "[870,330][930,390]",
                rid="jp.naver.line.android:id/unread_mark_count",
            ),
        ]),
    )
    rows, dbg = parse_unread_rows(xml)
    assert len(rows) == 1, dbg
    assert rows[0].name == "Alice"


def test_parse_unread_rows_max_limit():
    xml = _chat_list_xml_with_unread(
        [(f"U{i}", 1, 200 + i * 180) for i in range(8)]
    )
    rows, _ = parse_unread_rows(xml, max_rows=3)
    assert len(rows) == 3


# ───── ui_hierarchy.find_topbar_title ─────

def test_find_topbar_title_by_rid():
    xml = _hier(
        "jp.naver.line.android",
        _node_textview(
            "小王",
            "[120,60][600,160]",
            rid="jp.naver.line.android:id/header_title",
        ),
    )
    name, dbg = ui.find_topbar_title(xml)
    assert name == "小王", dbg


def test_find_topbar_title_fallback_by_position():
    # 没有 hint rid，但在顶部区域只有一个 TextView
    xml = _hier(
        "jp.naver.line.android",
        _node_textview("张三", "[120,40][500,130]", rid=""),
    )
    name, dbg = ui.find_topbar_title(xml)
    assert name == "张三", dbg


def test_has_back_button_true_and_false():
    xml_with = _hier(
        "jp.naver.line.android",
        _node_textview("", "[0,40][80,120]", cdesc="Navigate up"),
    )
    xml_without = _hier(
        "jp.naver.line.android",
        _node_textview("foo", "[0,40][80,120]"),
    )
    assert ui.has_back_button(xml_with) is True
    assert ui.has_back_button(xml_without) is False


# ───── runner._row_allowed ─────

def _mk_row(name: str) -> UnreadRow:
    return UnreadRow(
        name=name, unread_count=1, tap_x=500, tap_y=400,
        bounds=(0, 300, 1000, 500), badge_bounds=(900, 330, 980, 400),
        name_rid="", badge_rid="",
    )


@pytest.mark.parametrize("name,allow,deny,expect", [
    ("Alice", [], [], True),
    ("Alice", ["Ali"], [], True),
    ("Alice", ["Bob"], [], False),
    ("Alice", [], ["Alice"], False),
    ("Alice", ["Ali"], ["Alice"], False),  # deny 优先
    ("群公告", [], ["官方", "通知", "公告"], False),
    ("测试群", ["测试"], ["官方"], True),
])
def test_row_allowed(name, allow, deny, expect):
    row = _mk_row(name)
    ok, _ = LineRpaRunner._row_allowed(row, allow, deny)
    assert ok is expect
