"""ASR 幻觉守卫门禁。

根因回归：主 ASR（Qwen3-ASR）不可达时回落 faster-whisper，后者在**静音/噪声/极短**片段上
会幻觉出训练语料的尾字幕套话（"请点赞订阅转发"/"谢谢观看"/"字幕组"…）——与客户所说毫无关系，
当真回复必然驴唇不对马嘴。守卫在**转录出口**丢弃这类无歧义幻觉（等同空结果，交上层回落
media_ack），且**绝不误伤正常闲聊**（单字"林"/"好的"/笑声"哈哈哈"照常放行）。
"""
from __future__ import annotations

import pytest

from src.voice_transcriber import VoiceTranscriber, looks_like_asr_hallucination


# ── 纯函数：命中幻觉套话 ───────────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "请不吝点赞订阅转发打赏支持明镜与点点栏目",
    "谢谢观看",
    "谢谢大家观看，我们下期再见",
    "字幕组",
    "本视频字幕由字幕志愿者提供",
    "感谢观看",
    "Thanks for watching!",
    "please subscribe to my channel",
    "subtitles by amara.org",
    "。" * 15,          # 退化：单字符霸屏（≥12 且 ≥90%）
    "啊啊啊啊啊啊啊啊啊啊啊啊啊啊",  # 14 个"啊" → 退化
])
def test_detects_hallucination(text):
    assert looks_like_asr_hallucination(text) is True


# ── 纯函数：绝不误伤正常内容（保守）─────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "林",                      # 单字叫名（用户报的正例：不能被吞）
    "好的",
    "在的呀，怎么啦",
    "你中午吃饭了吗",
    "哈哈哈哈哈哈",             # 笑声（<12 字）→ 放行
    "我想听听你的声音",
    "十五岁了",
    "护士正在给我包扎",
    "谢谢你一直陪着我",          # 含"谢谢"但非"谢谢观看"套话 → 放行
    "",
    "   ",
])
def test_does_not_flag_legit(text):
    assert looks_like_asr_hallucination(text) is False


def test_none_safe():
    assert looks_like_asr_hallucination(None) is False  # type: ignore[arg-type]


# ── 端到端：守卫在 transcribe_voice_message 出口生效 ───────────────────────────
class _FakeTranscriber(VoiceTranscriber):
    def __init__(self, config, canned):
        super().__init__(config)
        self._canned = canned

    async def _transcribe_impl(self, voice_file_path, language):
        return self._canned


def _voice_file(tmp_path):
    p = tmp_path / "v.ogg"
    p.write_bytes(b"\x00\x01\x02\x03")  # 内容无所谓，_transcribe_impl 被打桩
    return str(p)


async def test_guard_drops_hallucination(tmp_path):
    t = _FakeTranscriber({"temp_dir": str(tmp_path / "t")}, "谢谢观看")
    assert await t.transcribe_voice_message(_voice_file(tmp_path)) is None


async def test_guard_keeps_legit_short(tmp_path):
    t = _FakeTranscriber({"temp_dir": str(tmp_path / "t")}, "林")
    assert await t.transcribe_voice_message(_voice_file(tmp_path)) == "林"


async def test_guard_disabled_passes_through(tmp_path):
    # hallucination_guard=false → 退回旧行为（即便是幻觉套话也照常返回）
    t = _FakeTranscriber(
        {"temp_dir": str(tmp_path / "t"), "hallucination_guard": False}, "谢谢观看")
    assert await t.transcribe_voice_message(_voice_file(tmp_path)) == "谢谢观看"


async def test_guard_normal_reply_unaffected(tmp_path):
    t = _FakeTranscriber({"temp_dir": str(tmp_path / "t")}, "  你中午吃饭了吗  ")
    assert await t.transcribe_voice_message(_voice_file(tmp_path)) == "你中午吃饭了吗"
