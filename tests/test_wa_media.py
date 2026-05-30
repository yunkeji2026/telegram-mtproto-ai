"""WhatsApp 媒体消息检测 + media_vision 单元测试 (P5)。"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ─────────────────────────────────────────────────────────────────────────────
# XML helpers
# ─────────────────────────────────────────────────────────────────────────────

def _xml(*nodes: str) -> bytes:
    inner = "\n".join(nodes)
    return f'<hierarchy rotation="0">{inner}</hierarchy>'.encode("utf-8")


_IMG_NODE = (
    '<node bounds="[0,300][360,480]" resource-id="com.whatsapp:id/image_thumb"'
    ' content-desc="照片" class="android.widget.ImageView" />'
)
_VIDEO_NODE = (
    '<node bounds="[0,500][360,680]" resource-id="com.whatsapp:id/video_thumb"'
    ' content-desc="视频" class="android.widget.ImageView" />'
)
_STICKER_NODE = (
    '<node bounds="[0,700][200,880]" resource-id="com.whatsapp:id/sticker_view"'
    ' content-desc="贴纸" class="android.widget.ImageView" />'
)
_GIF_NODE = (
    '<node bounds="[0,900][360,1060]" resource-id="com.whatsapp:id/gif_view"'
    ' content-desc="GIF" class="android.widget.ImageView" />'
)
_FILE_NODE = (
    '<node bounds="[0,1100][360,1200]" resource-id="com.whatsapp:id/document_thumb"'
    ' content-desc="文件" class="android.widget.ImageView" />'
)
_VOICE_NODE = (
    '<node bounds="[0,200][360,280]" resource-id="com.whatsapp:id/control_btn"'
    ' content-desc="播放 语音消息" class="android.widget.ImageButton" />'
)
_OUTGOING_IMG = (
    '<node bounds="[380,300][720,480]" resource-id="com.whatsapp:id/image_thumb"'
    ' content-desc="照片" class="android.widget.ImageView" />'
)


# ─────────────────────────────────────────────────────────────────────────────
# ui_hierarchy — detect_media_messages
# ─────────────────────────────────────────────────────────────────────────────

class TestDetectMediaMessages:
    def test_detects_image(self):
        from src.integrations.whatsapp_rpa.ui_hierarchy import detect_media_messages
        xml = _xml(_IMG_NODE)
        msgs = detect_media_messages(xml, screen_width=720)
        assert any(m.kind == "image" for m in msgs)

    def test_detects_video(self):
        from src.integrations.whatsapp_rpa.ui_hierarchy import detect_media_messages
        xml = _xml(_VIDEO_NODE)
        msgs = detect_media_messages(xml, screen_width=720)
        assert any(m.kind == "video" for m in msgs)

    def test_detects_sticker(self):
        from src.integrations.whatsapp_rpa.ui_hierarchy import detect_media_messages
        xml = _xml(_STICKER_NODE)
        msgs = detect_media_messages(xml, screen_width=720)
        assert any(m.kind == "sticker" for m in msgs)

    def test_detects_gif(self):
        from src.integrations.whatsapp_rpa.ui_hierarchy import detect_media_messages
        xml = _xml(_GIF_NODE)
        msgs = detect_media_messages(xml, screen_width=720)
        assert any(m.kind == "gif" for m in msgs)

    def test_detects_file(self):
        from src.integrations.whatsapp_rpa.ui_hierarchy import detect_media_messages
        xml = _xml(_FILE_NODE)
        msgs = detect_media_messages(xml, screen_width=720)
        assert any(m.kind == "file" for m in msgs)

    def test_excludes_voice(self):
        from src.integrations.whatsapp_rpa.ui_hierarchy import detect_media_messages
        xml = _xml(_VOICE_NODE)
        msgs = detect_media_messages(xml, screen_width=720)
        assert len(msgs) == 0

    def test_incoming_outgoing_split(self):
        from src.integrations.whatsapp_rpa.ui_hierarchy import detect_media_messages
        xml = _xml(_IMG_NODE, _OUTGOING_IMG)
        msgs = detect_media_messages(xml, screen_width=720)
        incoming = [m for m in msgs if m.is_incoming]
        outgoing = [m for m in msgs if not m.is_incoming]
        assert len(incoming) >= 1
        assert len(outgoing) >= 1

    def test_sorted_by_bottom_y(self):
        from src.integrations.whatsapp_rpa.ui_hierarchy import detect_media_messages
        xml = _xml(_IMG_NODE, _VIDEO_NODE, _STICKER_NODE)
        msgs = detect_media_messages(xml, screen_width=720)
        bottoms = [m.bottom_y for m in msgs]
        assert bottoms == sorted(bottoms)

    def test_empty_xml(self):
        from src.integrations.whatsapp_rpa.ui_hierarchy import detect_media_messages
        assert detect_media_messages(b"", screen_width=720) == []

    def test_malformed_xml(self):
        from src.integrations.whatsapp_rpa.ui_hierarchy import detect_media_messages
        assert detect_media_messages(b"<not valid", screen_width=720) == []


class TestDetectLastIncomingMedia:
    def test_returns_last_incoming(self):
        from src.integrations.whatsapp_rpa.ui_hierarchy import detect_last_incoming_media
        xml = _xml(_IMG_NODE, _VIDEO_NODE)
        m = detect_last_incoming_media(xml, screen_width=720)
        assert m is not None
        assert m.is_incoming

    def test_returns_none_when_no_incoming(self):
        from src.integrations.whatsapp_rpa.ui_hierarchy import detect_last_incoming_media
        xml = _xml(_OUTGOING_IMG)
        m = detect_last_incoming_media(xml, screen_width=720)
        assert m is None

    def test_returns_none_for_empty_xml(self):
        from src.integrations.whatsapp_rpa.ui_hierarchy import detect_last_incoming_media
        assert detect_last_incoming_media(b"<hierarchy/>", screen_width=720) is None

    def test_voice_not_counted(self):
        from src.integrations.whatsapp_rpa.ui_hierarchy import detect_last_incoming_media
        xml = _xml(_VOICE_NODE)
        assert detect_last_incoming_media(xml, screen_width=720) is None


# ─────────────────────────────────────────────────────────────────────────────
# media_vision — media_placeholder
# ─────────────────────────────────────────────────────────────────────────────

class TestMediaPlaceholder:
    def test_image_zh(self):
        from src.integrations.whatsapp_rpa.media_vision import media_placeholder
        ph = media_placeholder("image", lang="zh")
        assert "图片" in ph

    def test_image_en(self):
        from src.integrations.whatsapp_rpa.media_vision import media_placeholder
        ph = media_placeholder("image", lang="en")
        assert "image" in ph.lower()

    def test_sticker_zh(self):
        from src.integrations.whatsapp_rpa.media_vision import media_placeholder
        ph = media_placeholder("sticker", lang="zh")
        assert "贴纸" in ph

    def test_video_with_duration(self):
        from src.integrations.whatsapp_rpa.media_vision import media_placeholder
        ph = media_placeholder("video", lang="zh", duration_text="0:12")
        assert "0:12" in ph

    def test_video_without_duration(self):
        from src.integrations.whatsapp_rpa.media_vision import media_placeholder
        ph = media_placeholder("video", lang="zh", duration_text="")
        assert "视频" in ph

    def test_gif_zh(self):
        from src.integrations.whatsapp_rpa.media_vision import media_placeholder
        ph = media_placeholder("gif", lang="zh")
        assert "GIF" in ph

    def test_file_zh(self):
        from src.integrations.whatsapp_rpa.media_vision import media_placeholder
        ph = media_placeholder("file", lang="zh")
        assert "文件" in ph

    def test_unknown_kind_fallback(self):
        from src.integrations.whatsapp_rpa.media_vision import media_placeholder
        ph = media_placeholder("unknown_kind", lang="zh")
        assert ph  # not empty


# ─────────────────────────────────────────────────────────────────────────────
# runner — get_media_metrics
# ─────────────────────────────────────────────────────────────────────────────

class TestMediaMetrics:
    def _make_runner(self):
        from src.integrations.whatsapp_rpa.runner import WhatsAppRpaRunner
        r = WhatsAppRpaRunner.__new__(WhatsAppRpaRunner)
        r._media_metrics = {
            "detected": 0, "vision_attempts": 0, "vision_ok": 0,
            "vision_fail": 0, "placeholder": 0,
            "kind_image": 0, "kind_video": 0, "kind_gif": 0,
            "kind_sticker": 0, "kind_file": 0, "kind_other": 0,
        }
        return r

    def test_initial_all_zero(self):
        r = self._make_runner()
        m = r.get_media_metrics()
        assert m["detected"] == 0
        assert m["vision_ok"] == 0

    def test_returns_copy(self):
        r = self._make_runner()
        m = r.get_media_metrics()
        m["detected"] = 999
        assert r.get_media_metrics()["detected"] == 0

    def test_increment_and_read(self):
        r = self._make_runner()
        r._media_metrics["detected"] += 2
        r._media_metrics["kind_image"] += 1
        r._media_metrics["vision_ok"] += 1
        m = r.get_media_metrics()
        assert m["detected"] == 2
        assert m["kind_image"] == 1
        assert m["vision_ok"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# runner — _try_describe_media disabled when media_input.enabled=False
# ─────────────────────────────────────────────────────────────────────────────

class TestTryDescribeMediaDisabled:
    @pytest.mark.asyncio
    async def test_returns_none_when_disabled(self):
        from src.integrations.whatsapp_rpa.runner import WhatsAppRpaRunner
        r = WhatsAppRpaRunner.__new__(WhatsAppRpaRunner)
        r._cfg = {"media_input": {"enabled": False}}
        r._media_metrics = {
            "detected": 0, "vision_attempts": 0, "vision_ok": 0,
            "vision_fail": 0, "placeholder": 0,
            "kind_image": 0, "kind_video": 0, "kind_gif": 0,
            "kind_sticker": 0, "kind_file": 0, "kind_other": 0,
        }
        r._serial = None

        def _cfg_get(key, default=None):
            return r._cfg.get(key, default)
        r._cfg_get = _cfg_get

        xml = _xml(_IMG_NODE)
        result = {}
        out = await r._try_describe_media(xml, 720, result)
        assert out is None
        assert r._media_metrics["detected"] == 0

    @pytest.mark.asyncio
    async def test_returns_none_for_no_media(self):
        from src.integrations.whatsapp_rpa.runner import WhatsAppRpaRunner
        r = WhatsAppRpaRunner.__new__(WhatsAppRpaRunner)
        r._cfg = {"media_input": {"enabled": True}}
        r._media_metrics = {
            "detected": 0, "vision_attempts": 0, "vision_ok": 0,
            "vision_fail": 0, "placeholder": 0,
            "kind_image": 0, "kind_video": 0, "kind_gif": 0,
            "kind_sticker": 0, "kind_file": 0, "kind_other": 0,
        }
        r._serial = None

        def _cfg_get(key, default=None):
            return r._cfg.get(key, default)
        r._cfg_get = _cfg_get

        xml = _xml(_VOICE_NODE)  # 只有语音，无媒体
        result = {}
        out = await r._try_describe_media(xml, 720, result)
        assert out is None

    @pytest.mark.asyncio
    async def test_placeholder_when_vision_disabled(self):
        from src.integrations.whatsapp_rpa.runner import WhatsAppRpaRunner
        r = WhatsAppRpaRunner.__new__(WhatsAppRpaRunner)
        r._cfg = {"media_input": {"enabled": True, "use_vision": False}}
        r._media_metrics = {
            "detected": 0, "vision_attempts": 0, "vision_ok": 0,
            "vision_fail": 0, "placeholder": 0,
            "kind_image": 0, "kind_video": 0, "kind_gif": 0,
            "kind_sticker": 0, "kind_file": 0, "kind_other": 0,
        }
        r._serial = "device123"

        def _cfg_get(key, default=None):
            return r._cfg.get(key, default)
        r._cfg_get = _cfg_get

        xml = _xml(_IMG_NODE)
        result = {}
        out = await r._try_describe_media(xml, 720, result)
        assert out is not None
        assert "图片" in out
        assert result.get("media_placeholder") is True
        assert r._media_metrics["placeholder"] == 1
        assert r._media_metrics["detected"] == 1
