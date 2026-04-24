"""知识库直接输出渲染（无 AI）"""
import json
import pytest
from pathlib import Path

pytestmark = pytest.mark.asyncio


@pytest.fixture
def cfg_dir():
    p = Path(__file__).resolve().parent.parent / "config"
    if not p.exists():
        pytest.skip("config not found")
    return p


async def test_legacy_no_spec(cfg_dir):
    from src.utils.kb_direct_render import render_kb_direct_reply

    e = {
        "id": "x",
        "title": "t",
        "example_reply_zh": "only\n---\nother",
        "reply_direct_spec": "",
    }
    text, meta = await render_kb_direct_reply(e, "hi", cfg_dir, None)
    assert text in ("only", "other")
    assert "legacy" in meta.get("path", [])


async def test_branch_and_placeholder(cfg_dir):
    from src.utils.kb_direct_render import render_kb_direct_reply

    spec = {
        "version": 1,
        "default_channel_key": "jc",
        "branches": {
            "normal": "OK {channel_display_name}",
        },
        "default_branch": "normal",
    }
    e = {
        "id": "y",
        "title": "t",
        "example_reply_zh": "fb",
        "reply_direct_spec": json.dumps(spec, ensure_ascii=False),
    }
    text, meta = await render_kb_direct_reply(e, "noop", cfg_dir, None)
    assert "JC" in text or "通道" in text
    assert meta.get("branch") == "normal"


async def test_fee_placeholder_not_raw_percent(cfg_dir):
    """KB 直接渲染占位符不得透出后台 fee_rate 数值。"""
    from src.utils.kb_direct_render import render_kb_direct_reply

    spec = {
        "version": 1,
        "default_channel_key": "ep",
        "branches": {
            "normal": "费率说明：{channel_fee_rate}（{channel_fee_description}）",
        },
        "default_branch": "normal",
    }
    e = {
        "id": "feeph",
        "title": "t",
        "example_reply_zh": "fb",
        "reply_direct_spec": json.dumps(spec, ensure_ascii=False),
    }
    text, _ = await render_kb_direct_reply(e, "ep 通道", cfg_dir, None)
    assert "0.5" not in text and "0.6" not in text
    assert "业务主管" in text or "人工客服" in text
