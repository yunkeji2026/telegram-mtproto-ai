"""line_rpa UI 解析单测（无设备）。"""

from src.integrations.line_rpa import ui_hierarchy as ui


_MINIMAL_XML = b"""<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node bounds="[0,0][1080,2400]" />
  <node class="android.widget.TextView" text="CHATS" bounds="[0,0][200,80]" />
  <node class="android.widget.TextView" text="Hello peer" resource-id="jp.naver.line.android:id/chat_row"
         bounds="[40,1800][600,1880]" />
  <node class="android.widget.TextView" text="Me reply" resource-id="jp.naver.line.android:id/chat_row"
         bounds="[500,2000][1040,2080]" />
  <node class="android.widget.EditText" text="" bounds="[80,2200][900,2340]" />
</hierarchy>
"""


def test_pick_last_peer_text_left():
    text, dbg = ui.pick_last_peer_text(_MINIMAL_XML, left_ratio=0.45)
    assert text == "Hello peer"
    assert "left_bubble" in dbg or "bottom=" in dbg


def test_find_edittext():
    xy = ui.find_edittext_bottom_center(_MINIMAL_XML)
    assert xy is not None
    assert xy[1] > 2000


# ── P7：LINE 内置搜索入口定位 ─────────────────────────────────────────────────

def _list_with(node: str) -> bytes:
    return (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>\n"
        "<hierarchy rotation='0'>\n"
        "  <node bounds='[0,0][1080,2340]' />\n"
        f"{node}\n"
        "</hierarchy>\n"
    ).encode("utf-8")


def test_find_search_entry_by_resource_id():
    xml = _list_with(
        "<node resource-id='jp.naver.line.android:id/search_bar' "
        "class='android.widget.EditText' bounds='[0,90][1080,190]' />"
    )
    xy = ui.find_search_entry(xml)
    assert xy == (540, 140)


def test_find_search_entry_by_content_desc():
    xml = _list_with(
        "<node content-desc='Search' class='android.widget.ImageView' "
        "bounds='[960,90][1060,190]' />"
    )
    xy = ui.find_search_entry(xml)
    assert xy is not None and xy[1] == 140


def test_find_search_entry_ignores_low_elements():
    """会话内/底部的 search 元素不应被当成列表顶部入口（避免误点）。"""
    xml = _list_with(
        "<node resource-id='jp.naver.line.android:id/msg_search' "
        "class='android.widget.EditText' bounds='[0,2000][1080,2100]' />"
    )
    assert ui.find_search_entry(xml) is None


def test_find_search_entry_none_when_absent():
    xml = _list_with(
        "<node class='android.widget.TextView' text='Alice' bounds='[60,300][500,380]' />"
    )
    assert ui.find_search_entry(xml) is None


def test_find_search_entry_broken_xml():
    assert ui.find_search_entry(b"not xml") is None
