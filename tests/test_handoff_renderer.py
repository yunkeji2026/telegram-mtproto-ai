"""HandoffRenderer 单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.skills.handoff_renderer import (
    HandoffRenderer,
    HandoffRendererError,
    CONTEXT_GOODBYE,
    CONTEXT_IDENTITY_ASKED,
    CONTEXT_ANY,
)


CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "handoff_scripts.yaml"


@pytest.fixture
def renderer():
    return HandoffRenderer(CONFIG_PATH)


class TestLoad:
    def test_loaded(self, renderer):
        assert renderer.count() >= 10
        # 里面至少包含 zh 和 en
        ids = renderer.list_ids()
        assert any(i.startswith("zh_") for i in ids)
        assert any(i.startswith("en_") for i in ids)

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(HandoffRendererError):
            HandoffRenderer(tmp_path / "nonexistent.yaml")

    def test_reload_after_mtime_change(self, tmp_path):
        p = tmp_path / "s.yaml"
        p.write_text(
            "scripts:\n  - id: x\n    language: zh\n    triggers: [any]\n"
            "    shell: {greeting: 'a', reason: 'b', cta: 'c {LINE_ID} {TOKEN}'}\n",
            encoding="utf-8",
        )
        r = HandoffRenderer(p)
        assert r.count() == 1
        # 改写文件
        import time
        time.sleep(0.02)
        p.write_text(
            "scripts:\n"
            "  - id: x\n    language: zh\n    triggers: [any]\n"
            "    shell: {greeting: 'a', reason: 'b', cta: 'c {LINE_ID} {TOKEN}'}\n"
            "  - id: y\n    language: zh\n    triggers: [any]\n"
            "    shell: {greeting: 'a', reason: 'b', cta: 'c {LINE_ID} {TOKEN}'}\n",
            encoding="utf-8",
        )
        # 主动 trigger reload
        import os
        os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 5))
        assert r.maybe_reload() is True
        assert r.count() == 2


class TestPick:
    def test_pick_zh_goodbye(self, renderer):
        s = renderer.pick(language="zh", context=CONTEXT_GOODBYE)
        assert s is not None
        assert s.language == "zh"
        assert CONTEXT_GOODBYE in [t.lower() for t in s.triggers] or \
               CONTEXT_ANY in [t.lower() for t in s.triggers]

    def test_pick_en_identity(self, renderer):
        s = renderer.pick(language="en", context=CONTEXT_IDENTITY_ASKED)
        assert s is not None
        assert s.language == "en"

    def test_pick_excludes(self, renderer):
        # 选 10 次，每次 exclude 上一次的 id，结果应有多样性
        seen = set()
        excl: list = []
        for _ in range(6):
            s = renderer.pick(language="zh", context=CONTEXT_ANY, exclude_ids=excl)
            if s is None:
                break
            seen.add(s.id)
            excl.append(s.id)
        assert len(seen) >= 3   # 至少选到 3 种

    def test_pick_unknown_language_none(self, renderer):
        assert renderer.pick(language="zz", context=CONTEXT_ANY) is None

    def test_tone_filter_with_fallback(self, renderer):
        """tone 指定了一个稀有 tone，应该 fallback 到不带 tone 的匹配。"""
        s = renderer.pick(language="zh", context=CONTEXT_GOODBYE, tone="nonexistent")
        assert s is not None   # fallback 应返回某条

    def test_context_fallback_to_any(self, tmp_path):
        # 一个只有 any triggers 的池，请求 goodbye 时应返回 any
        p = tmp_path / "s.yaml"
        p.write_text(
            "scripts:\n  - id: a\n    language: zh\n    triggers: [any]\n"
            "    shell: {greeting: 'g', reason: 'r', cta: 'c {LINE_ID} {TOKEN}'}\n",
            encoding="utf-8",
        )
        r = HandoffRenderer(p)
        s = r.pick(language="zh", context=CONTEXT_GOODBYE)
        assert s is not None
        assert s.id == "a"


class TestRender:
    def test_render_fills_slots(self, renderer):
        s = renderer.by_id("zh_phone_dying")
        assert s is not None
        rendered = renderer.render(s, line_id="alice123", token="m7ra2k")
        assert "alice123" in rendered.text
        assert "m7ra2k" in rendered.text
        assert "{LINE_ID}" not in rendered.text
        assert "{TOKEN}" not in rendered.text
        assert rendered.warning == ""

    def test_render_warns_if_missing_slot(self, tmp_path):
        p = tmp_path / "s.yaml"
        p.write_text(
            "scripts:\n  - id: bad\n    language: zh\n    triggers: [any]\n"
            "    shell: {greeting: 'g', reason: 'r', cta: 'no slot here'}\n",
            encoding="utf-8",
        )
        r = HandoffRenderer(p)
        s = r.by_id("bad")
        rendered = r.render(s, line_id="x", token="y")
        assert rendered.warning == "script_missing_LINE_ID_slot"

    def test_render_preserves_language(self, renderer):
        s = renderer.by_id("en_phone_dying")
        rendered = renderer.render(s, line_id="L", token="T")
        assert rendered.language == "en"
        assert "add me on line" in rendered.text.lower()


class TestMalformed:
    def test_empty_file_raises(self, tmp_path):
        p = tmp_path / "empty.yaml"
        p.write_text("", encoding="utf-8")
        with pytest.raises(HandoffRendererError):
            HandoffRenderer(p)

    def test_skips_malformed_entries(self, tmp_path):
        p = tmp_path / "mixed.yaml"
        p.write_text(
            "scripts:\n"
            "  - id: good\n    language: zh\n    triggers: [any]\n"
            "    shell: {greeting: a, reason: b, cta: 'c {LINE_ID} {TOKEN}'}\n"
            "  - id: bad_no_triggers\n    language: zh\n    shell: {}\n"
            "  - id: bad_no_lang\n    triggers: [any]\n    shell: {}\n",
            encoding="utf-8",
        )
        r = HandoffRenderer(p)
        assert r.count() == 1
        assert r.by_id("good") is not None
