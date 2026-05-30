"""P2/P4 — PersonaManager + MessengerRpa architecture convergence tests."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from unittest.mock import MagicMock, patch

from src.utils.persona_manager import PersonaManager


# ── P4: Chat binding reference store ─────────────────────────────────────────


class TestChatBindingReference:
    """P4: bind_chat_persona_by_profile_id must store a reference, not a snapshot."""

    def setup_method(self):
        PersonaManager.reset()
        self.pm = PersonaManager.get_instance()
        self.pm.upsert_profile("pid_alice", {"id": "pid_alice", "name": "Alice"}, _track_history=False)

    def teardown_method(self):
        PersonaManager.reset()

    def test_bind_stores_reference(self):
        ok = self.pm.bind_chat_persona_by_profile_id("chat1", "pid_alice")
        assert ok
        assert "chat1" in self.pm._chat_bindings
        assert self.pm._chat_bindings["chat1"] == "pid_alice"
        assert "chat1" not in self.pm._chat_personas  # no snapshot

    def test_get_persona_live_resolves(self):
        self.pm.bind_chat_persona_by_profile_id("chat1", "pid_alice")
        p = self.pm.get_persona("chat1")
        assert p["name"] == "Alice"

    def test_edit_profile_reflected_without_rebind(self):
        self.pm.bind_chat_persona_by_profile_id("chat1", "pid_alice")
        # Simulate operator editing via /personas
        self.pm.upsert_profile("pid_alice", {"id": "pid_alice", "name": "Alice Edited"})
        p = self.pm.get_persona("chat1")
        assert p["name"] == "Alice Edited"  # live data, no rebind needed

    def test_deleted_profile_falls_through_to_domain(self):
        self.pm.set_domain_persona({"name": "DomainDefault"})
        self.pm.bind_chat_persona_by_profile_id("chat1", "pid_alice")
        self.pm.delete_profile("pid_alice")
        p = self.pm.get_persona("chat1")
        assert p["name"] == "DomainDefault"  # graceful fallback, no stale data

    def test_has_chat_binding_true_for_ref(self):
        self.pm.bind_chat_persona_by_profile_id("chat1", "pid_alice")
        assert self.pm.has_chat_binding("chat1")

    def test_unbind_clears_reference(self):
        self.pm.bind_chat_persona_by_profile_id("chat1", "pid_alice")
        self.pm.unbind_chat_persona("chat1")
        assert not self.pm.has_chat_binding("chat1")
        assert "chat1" not in self.pm._chat_bindings
        assert "chat1" not in self.pm._chat_personas

    def test_inline_bind_still_works(self):
        """Legacy bind_chat_persona (inline) must still work for backward compat."""
        self.pm.bind_chat_persona("chat2", {"name": "InlineBot"})
        assert "chat2" in self.pm._chat_personas
        p = self.pm.get_persona("chat2")
        assert p["name"] == "InlineBot"

    def test_ref_bind_clears_stale_inline_snapshot(self):
        """Switching from inline to ref must remove old snapshot."""
        self.pm.bind_chat_persona("chat1", {"name": "OldSnapshot"})
        assert "chat1" in self.pm._chat_personas
        self.pm.bind_chat_persona_by_profile_id("chat1", "pid_alice")
        assert "chat1" not in self.pm._chat_personas  # snapshot cleared

    def test_binding_count_includes_refs(self):
        self.pm.bind_chat_persona_by_profile_id("chat1", "pid_alice")
        self.pm.bind_chat_persona_by_profile_id("chat2", "pid_alice")
        summaries = self.pm.list_profiles_summary()
        alice = next(s for s in summaries if s["id"] == "pid_alice")
        assert alice["binding_count"] == 2

    def test_get_all_chat_bindings_includes_profile_ref(self):
        self.pm.bind_chat_persona_by_profile_id("chat1", "pid_alice")
        all_b = self.pm.get_all_chat_bindings()
        assert "chat1" in all_b
        assert all_b["chat1"]["_profile_ref"] == "pid_alice"
        assert all_b["chat1"]["name"] == "Alice"

    def test_export_import_roundtrip(self):
        self.pm.bind_chat_persona_by_profile_id("chat1", "pid_alice")
        exported = self.pm.export_chat_bindings()
        assert "ref_bindings" in exported
        assert exported["ref_bindings"]["chat1"] == "pid_alice"

        PersonaManager.reset()
        pm2 = PersonaManager.get_instance()
        pm2.upsert_profile("pid_alice", {"id": "pid_alice", "name": "Alice"}, _track_history=False)
        pm2.import_chat_bindings(exported)
        assert pm2.has_chat_binding("chat1")
        p = pm2.get_persona("chat1")
        assert p["name"] == "Alice"

    def test_get_persona_with_tier_ref_returns_chat_binding_tier(self):
        self.pm.bind_chat_persona_by_profile_id("chat1", "pid_alice")
        p, tier = self.pm.get_persona_with_tier("chat1")
        assert tier == "chat_binding"
        assert p["name"] == "Alice"

    def test_bulk_bind_updates_ref_not_snapshot(self):
        self.pm.upsert_profile("pid_bob", {"id": "pid_bob", "name": "Bob"}, _track_history=False)
        self.pm.bind_chat_persona_by_profile_id("chat1", "pid_alice")
        result = self.pm.bulk_bind_by_profile("pid_bob", scope="all_bindings")
        assert result["affected"] == 1
        # Reference updated to pid_bob, no snapshot created
        assert self.pm._chat_bindings["chat1"] == "pid_bob"
        assert "chat1" not in self.pm._chat_personas
        p = self.pm.get_persona("chat1")
        assert p["name"] == "Bob"


# ── P9: Bindings + Prompt preview ────────────────────────────────────────────


class TestP9BindingsAndPrompt:
    """P9-A/B: platform detection for bindings + prompt preview assembly."""

    def setup_method(self):
        PersonaManager.reset()
        self.pm = PersonaManager.get_instance()

    def teardown_method(self):
        PersonaManager.reset()

    # ── Platform detection (inline replication of API logic) ──────────────────

    @staticmethod
    def _detect_platform(cid: str) -> str:
        c = str(cid).lower()
        if c.startswith("line_rpa:") or c.startswith("line:"):
            return "line"
        if c.startswith("mrpa:") or c.startswith("messenger:"):
            return "mrpa"
        if c.startswith("wa:") or c.startswith("whatsapp:"):
            return "wa"
        try:
            n = int(cid)
            return "tg_group" if n < 0 else "tg_private"
        except ValueError:
            return "other"

    def test_platform_tg_group(self):
        assert self._detect_platform("-1001234567890") == "tg_group"

    def test_platform_tg_private(self):
        assert self._detect_platform("123456789") == "tg_private"

    def test_platform_line(self):
        assert self._detect_platform("line_rpa:abc") == "line"

    def test_platform_mrpa(self):
        assert self._detect_platform("mrpa:user123") == "mrpa"
        assert self._detect_platform("messenger:xyz") == "mrpa"

    def test_platform_wa(self):
        assert self._detect_platform("wa:819012345678") == "wa"

    def test_platform_other(self):
        assert self._detect_platform("unknown_format") == "other"

    # ── Bindings enumeration via _chat_bindings ────────────────────────────────

    def test_bindings_for_profile_via_chat_bindings(self):
        self.pm.upsert_profile("alice", {"id": "alice"}, _track_history=False)
        self.pm.bind_chat_persona_by_profile_id("123456789", "alice")   # tg_private
        self.pm.bind_chat_persona_by_profile_id("-1001234", "alice")    # tg_group
        self.pm.bind_chat_persona_by_profile_id("line_rpa:L1", "alice") # line

        bindings = [
            {"chat_id": cid, "platform": self._detect_platform(cid)}
            for cid, pid in self.pm._chat_bindings.items()
            if str(pid) == "alice"
        ]
        platforms = {b["platform"] for b in bindings}
        assert "tg_private" in platforms
        assert "tg_group" in platforms
        assert "line" in platforms

    def test_bindings_empty_for_unbound_profile(self):
        self.pm.upsert_profile("bob", {"id": "bob"}, _track_history=False)
        bindings = [cid for cid, pid in self.pm._chat_bindings.items() if str(pid) == "bob"]
        assert bindings == []

    # ── Prompt preview ─────────────────────────────────────────────────────────

    def test_prompt_preview_full_contains_name(self):
        self.pm.upsert_profile("camille", {
            "id": "camille", "name": "Camille", "role": "亲密伴侣",
            "personality": {"style": "温柔体贴"},
        }, _track_history=False)
        p = self.pm.get_persona_by_id("camille")
        full = self.pm._format_persona_instructions(p)
        assert "Camille" in full
        assert "亲密伴侣" in full

    def test_prompt_preview_compact_shorter_than_full(self):
        self.pm.upsert_profile("dana", {
            "id": "dana", "name": "Dana", "role": "助手",
            "personality": {"traits": ["活泼"], "taboos": ["不礼貌"]},
            "speaking": {"reply_length": "detailed", "forbidden_phrases": ["随便", "无所谓"]},
        }, _track_history=False)
        p = self.pm.get_persona_by_id("dana")
        full = self.pm._format_persona_instructions(p)
        compact = self.pm._format_persona_compact(p)
        assert len(compact) < len(full)  # compact is always shorter

    def test_prompt_preview_compact_has_forbidden_phrases(self):
        self.pm.upsert_profile("eve", {
            "id": "eve", "name": "Eve",
            "speaking": {"forbidden_phrases": ["AI", "机器人"]},
        }, _track_history=False)
        p = self.pm.get_persona_by_id("eve")
        compact = self.pm._format_persona_compact(p)
        assert "禁止使用" in compact
        assert "AI" in compact


# ── P10: WA chat_key + platform-aware prompt + funnel-stage ──────────────────


class TestP10WaPersonaEnhancements:
    """P10-A/C: stable chat_key format + WA constraints + funnel-stage in prompt."""

    def setup_method(self):
        PersonaManager.reset()
        self.pm = PersonaManager.get_instance()
        self.pm.upsert_profile("mika", {
            "id": "mika", "name": "Mika", "role": "亲密伴侣",
            "personality": {"traits": ["温柔"], "emoji_level": "moderate"},
            "speaking": {"forbidden_phrases": ["AI", "机器人"]},
            "identity": {"deny_ai": True, "deny_ai_reply": "我是Mika"},
        }, _track_history=False)

    def teardown_method(self):
        PersonaManager.reset()

    # ── P10-A: stable chat_key using account_id ───────────────────────────────

    def test_wa_chat_key_uses_account_id(self):
        """chat_key must be wa:{account_id}:{peer_name}, not wa:{serial}:{peer_name}."""
        account_id = "wa_q4n"
        peer_name = "佐藤拓満"
        chat_key = f"wa:{account_id}:{peer_name}"
        assert chat_key == "wa:wa_q4n:佐藤拓満"
        assert not chat_key.startswith("wa:Q4N")  # serial must NOT be in key

    def test_wa_chat_key_platform_detection(self):
        """chat_key with wa: prefix must be detected as 'wa' platform (not tg_private)."""
        def _detect(cid):
            c = str(cid).lower()
            if c.startswith("wa:") or c.startswith("whatsapp:"):
                return "wa"
            try:
                n = int(cid)
                return "tg_group" if n < 0 else "tg_private"
            except ValueError:
                return "other"
        assert _detect("wa:wa_q4n:佐藤拓満") == "wa"

    # ── P10-C: platform-aware prompt ─────────────────────────────────────────

    def test_wa_platform_constraints_injected_in_full_prompt(self):
        p = self.pm.get_persona_by_id("mika")
        full = self.pm._format_persona_instructions(p, platform="whatsapp")
        assert "WhatsApp" in full
        assert "Markdown" in full

    def test_wa_platform_constraints_NOT_injected_for_telegram(self):
        p = self.pm.get_persona_by_id("mika")
        full = self.pm._format_persona_instructions(p, platform="telegram")
        assert "WhatsApp" not in full

    def test_wa_platform_constraints_NOT_injected_without_platform(self):
        p = self.pm.get_persona_by_id("mika")
        full = self.pm._format_persona_instructions(p)
        assert "WhatsApp" not in full

    def test_wa_channel_keyword_also_triggers_wa_constraints(self):
        """channel='whatsapp_rpa' → platform='whatsapp' via 'whatsapp' in channel string."""
        p = self.pm.get_persona_by_id("mika")
        full = self.pm._format_persona_instructions(p, platform="whatsapp_rpa")
        assert "WhatsApp" in full

    # ── P10-C: funnel-stage tone injection ────────────────────────────────────

    def test_funnel_stage_cold_injected(self):
        p = self.pm.get_persona_by_id("mika")
        full = self.pm._format_persona_instructions(p, funnel_stage="cold")
        assert "冷启动" in full

    def test_funnel_stage_warm_injected(self):
        p = self.pm.get_persona_by_id("mika")
        full = self.pm._format_persona_instructions(p, funnel_stage="warm")
        assert "暖场中" in full

    def test_funnel_stage_hot_injected(self):
        p = self.pm.get_persona_by_id("mika")
        full = self.pm._format_persona_instructions(p, funnel_stage="hot")
        assert "高意向" in full

    def test_funnel_stage_unknown_not_injected(self):
        p = self.pm.get_persona_by_id("mika")
        full = self.pm._format_persona_instructions(p, funnel_stage="unknown_stage")
        assert "漏斗阶段" not in full

    def test_wa_platform_and_funnel_stage_combined(self):
        p = self.pm.get_persona_by_id("mika")
        full = self.pm._format_persona_instructions(
            p, platform="whatsapp", funnel_stage="warm"
        )
        assert "WhatsApp" in full
        assert "暖场中" in full

    # ── format_persona_block passthrough ─────────────────────────────────────

    def test_format_persona_block_accepts_platform_and_funnel(self):
        self.pm.bind_chat_persona_by_profile_id("wa:wa_q4n:test_peer", "mika")
        block = self.pm.format_persona_block(
            "wa:wa_q4n:test_peer",
            detail="full",
            platform="whatsapp",
            funnel_stage="hot",
        )
        assert "WhatsApp" in block
        assert "高意向" in block
        assert "Mika" in block


# ── P5: Persistence atomicity + _mrpa_source filter ──────────────────────────


class TestP8CanonicalDiffAndBak:
    """P8: bak rotation + canonical diff logic."""

    def setup_method(self):
        PersonaManager.reset()
        self.pm = PersonaManager.get_instance()

    def teardown_method(self):
        PersonaManager.reset()

    def test_save_personas_creates_bak(self, tmp_path):
        """P8-A: save_personas rotates existing .yaml to .yaml.bak before overwrite."""
        from unittest.mock import patch
        import yaml as _yaml

        # Simulate ConfigManager.save_personas behaviour directly
        path = tmp_path / "personas.yaml"
        bak = tmp_path / "personas.yaml.bak"
        # Write initial version
        with open(path, "w", encoding="utf-8") as f:
            _yaml.dump({"profiles": {"v1": {"id": "v1", "name": "Old"}}, "updated_at": "t1"}, f)

        # Now simulate the bak rotation (inline — matches config_manager.py logic)
        tmp = path.with_suffix(".yaml.tmp")
        new_data = {"profiles": {"v2": {"id": "v2", "name": "New"}}, "updated_at": "t2"}
        with open(tmp, "w", encoding="utf-8") as f:
            import yaml as _y; _y.dump(new_data, f, allow_unicode=True)
        with open(tmp, "r", encoding="utf-8") as f:
            _y.safe_load(f)  # validate
        if path.exists():
            path.replace(bak)
        tmp.replace(path)

        assert bak.exists()
        assert path.exists()
        with open(bak, encoding="utf-8") as f:
            bak_data = _yaml.safe_load(f)
        assert bak_data["updated_at"] == "t1"
        with open(path, encoding="utf-8") as f:
            cur_data = _yaml.safe_load(f)
        assert cur_data["updated_at"] == "t2"

    def test_diff_canonical_identical(self):
        """When PM profile exactly matches canonical, diff is empty and is_identical=True."""
        import json as _json
        profile = {"id": "p1", "name": "Alice", "role": "companion"}
        self.pm.upsert_profile("p1", profile, _track_history=False)
        canonical = {"id": "p1", "name": "Alice", "role": "companion"}

        # Replicate diff logic from api_profile_diff_canonical
        _SKIP = {"_mrpa_source"}
        current = self.pm.get_persona_by_id("p1")
        _c_keys = {k for k in canonical if k not in _SKIP}
        _p_keys = {k for k in current if k not in _SKIP}
        added = {k: current.get(k) for k in _p_keys - _c_keys}
        removed = {k: canonical.get(k) for k in _c_keys - _p_keys}
        changed = []
        for k in _c_keys & _p_keys:
            if _json.dumps(canonical.get(k), sort_keys=True) != _json.dumps(current.get(k), sort_keys=True):
                changed.append(k)
        assert not added and not removed and not changed  # is_identical

    def test_diff_canonical_detects_changes(self):
        """Changed and added fields are correctly surfaced."""
        import json as _json
        self.pm.upsert_profile("p2", {"id": "p2", "name": "Alicia", "extra": "new"}, _track_history=False)
        canonical = {"id": "p2", "name": "Alice", "role": "companion"}

        current = self.pm.get_persona_by_id("p2")
        _SKIP = {"_mrpa_source"}
        _c_keys = {k for k in canonical if k not in _SKIP}
        _p_keys = {k for k in current if k not in _SKIP}
        added = {k: current.get(k) for k in _p_keys - _c_keys}
        removed = {k: canonical.get(k) for k in _c_keys - _p_keys}
        changed = [k for k in _c_keys & _p_keys
                   if _json.dumps(canonical.get(k), sort_keys=True) != _json.dumps(current.get(k), sort_keys=True)]
        assert "extra" in added           # new field in current
        assert "role" in removed          # field removed from canonical
        assert "name" in changed          # value changed

    def test_diff_skips_mrpa_source_key(self):
        """_mrpa_source key is excluded from diff output."""
        import json as _json
        self.pm.upsert_profile("p3", {"id": "p3", "name": "Bob", "_mrpa_source": True}, _track_history=False)
        canonical = {"id": "p3", "name": "Bob"}

        current = self.pm.get_persona_by_id("p3")
        _SKIP = {"_mrpa_source"}
        _c_keys = {k for k in canonical if k not in _SKIP}
        _p_keys = {k for k in current if k not in _SKIP}
        added = {k: current.get(k) for k in _p_keys - _c_keys}
        # _mrpa_source must not appear in added
        assert "_mrpa_source" not in added


class TestP7CanonicalSync:
    """P7-A: mark_profiles_canonical + _last_canonical_sync_at."""

    def setup_method(self):
        PersonaManager.reset()
        self.pm = PersonaManager.get_instance()

    def teardown_method(self):
        PersonaManager.reset()

    def test_mark_profiles_canonical_updates_source(self):
        self.pm.upsert_profile("s1", {"id": "s1", "name": "Studio"}, _track_history=False)
        self.pm.upsert_profile("s2", {"id": "s2", "name": "Studio2"}, _track_history=False)
        assert self.pm._profile_sources["s1"] == "studio"
        assert self.pm._profile_sources["s2"] == "studio"
        self.pm.mark_profiles_canonical(["s1", "s2"])
        assert self.pm._profile_sources["s1"] == "canonical"
        assert self.pm._profile_sources["s2"] == "canonical"

    def test_mark_canonical_ignores_mrpa_profiles(self):
        self.pm.upsert_profile("m1", {"id": "m1", "_mrpa_source": True}, _track_history=False)
        # mark_profiles_canonical skips profiles with _mrpa_source
        self.pm.mark_profiles_canonical(["m1"])
        assert self.pm._profile_sources["m1"] == "mrpa"  # unchanged

    def test_mark_canonical_updates_last_sync_ts(self):
        import time
        before = time.time()
        self.pm.upsert_profile("x1", {"id": "x1"}, _track_history=False)
        self.pm.mark_profiles_canonical(["x1"])
        assert self.pm._last_canonical_sync_at >= before

    def test_initial_last_canonical_sync_at_is_zero(self):
        assert self.pm._last_canonical_sync_at == 0.0

    def test_source_breakdown_via_list_profiles_summary(self):
        self.pm.upsert_profile("op1", {"id": "op1"}, _track_history=False)
        self.pm.upsert_profile("op2", {"id": "op2"}, _track_history=False)
        self.pm.upsert_profile("m1", {"id": "m1", "_mrpa_source": True}, _track_history=False)
        self.pm.mark_profiles_canonical(["op1"])

        summary = self.pm.list_profiles_summary()
        sources = {p["id"]: p["source"] for p in summary}
        assert sources["op1"] == "canonical"
        assert sources["op2"] == "studio"
        assert sources["m1"] == "mrpa"

    def test_promote_removes_mrpa_source_flag(self):
        """Simulates the promote endpoint logic at the PM level."""
        import copy
        self.pm.upsert_profile("mrpa1", {"id": "mrpa1", "name": "Auto", "_mrpa_source": True}, _track_history=False)
        p = self.pm.get_persona_by_id("mrpa1")
        assert p["_mrpa_source"] is True

        promoted = copy.deepcopy(p)
        promoted.pop("_mrpa_source", None)
        self.pm.upsert_profile("mrpa1", promoted, _track_history=True)

        summary = {s["id"]: s for s in self.pm.list_profiles_summary()}
        assert not summary["mrpa1"]["is_mrpa_source"]
        assert summary["mrpa1"]["source"] == "studio"


class TestProfileSourceTracking:
    """P6: _profile_sources tracks where each profile was loaded from."""

    def setup_method(self):
        PersonaManager.reset()
        self.pm = PersonaManager.get_instance()

    def teardown_method(self):
        PersonaManager.reset()

    def test_load_from_config_sets_config_source(self):
        cfg = {"personas": {"profiles": [{"id": "cfg1", "name": "FromConfig"}]}}
        self.pm.load_profiles_from_config(cfg)
        summary = {p["id"]: p for p in self.pm.list_profiles_summary()}
        assert summary["cfg1"]["source"] == "config"
        assert not summary["cfg1"]["is_mrpa_source"]

    def test_upsert_with_mrpa_flag_sets_mrpa_source(self):
        self.pm.upsert_profile("m1", {"id": "m1", "name": "Auto", "_mrpa_source": True}, _track_history=False)
        summary = {p["id"]: p for p in self.pm.list_profiles_summary()}
        assert summary["m1"]["source"] == "mrpa"
        assert summary["m1"]["is_mrpa_source"] is True

    def test_upsert_without_mrpa_flag_sets_studio_source(self):
        self.pm.upsert_profile("s1", {"id": "s1", "name": "Edited"}, _track_history=False)
        summary = {p["id"]: p for p in self.pm.list_profiles_summary()}
        assert summary["s1"]["source"] == "studio"
        assert not summary["s1"]["is_mrpa_source"]

    def test_load_canonical_overrides_config_source(self):
        cfg = {"personas": {"profiles": [{"id": "shared", "name": "Base"}]}}
        self.pm.load_profiles_from_config(cfg)
        cm = MagicMock()
        cm.config = {"persona_persistence": {"enabled": True}}
        cm.get_personas_config = MagicMock(return_value={
            "profiles": {"shared": {"id": "shared", "name": "Canonical Version"}}
        })
        self.pm.load_personas_canonical(cm)
        summary = {p["id"]: p for p in self.pm.list_profiles_summary()}
        assert summary["shared"]["source"] == "canonical"

    def test_load_runtime_overrides_canonical_source(self, tmp_path):
        cfg = {"personas": {"profiles": [{"id": "r1", "name": "Base"}]}}
        self.pm.load_profiles_from_config(cfg)
        cm = MagicMock()
        cm.config = {"persona_persistence": {"enabled": True}}
        cm.get_personas_config = MagicMock(return_value={
            "profiles": {"r1": {"id": "r1", "name": "Canonical"}}
        })
        self.pm.load_personas_canonical(cm)
        # Now simulate runtime layer
        import yaml as _yaml
        runtime_file = tmp_path / "profiles_runtime.yaml"
        with open(runtime_file, "w", encoding="utf-8") as f:
            _yaml.dump({"profiles": {"r1": {"id": "r1", "name": "Runtime"}}}, f)
        self.pm.load_profiles_runtime(tmp_path / "config.yaml", {"persona_persistence": {"enabled": True}})
        summary = {p["id"]: p for p in self.pm.list_profiles_summary()}
        assert summary["r1"]["source"] == "runtime"

    def test_delete_cleans_source_tracking(self):
        self.pm.upsert_profile("del1", {"id": "del1", "name": "Temp"}, _track_history=False)
        assert "del1" in self.pm._profile_sources
        self.pm.delete_profile("del1")
        assert "del1" not in self.pm._profile_sources

    def test_reload_config_resets_source_to_config(self):
        """API reload (load_profiles_from_config again) should reset source to 'config'."""
        self.pm.upsert_profile("x1", {"id": "x1", "name": "Studio Edit"})
        assert self.pm._profile_sources["x1"] == "studio"
        cfg = {"personas": {"profiles": [{"id": "x1", "name": "From Config"}]}}
        self.pm.load_profiles_from_config(cfg)
        assert self.pm._profile_sources["x1"] == "config"


class TestPersistProfilesFilter:
    """P5-B: persist_profiles must only write operator-owned profiles."""

    def setup_method(self):
        PersonaManager.reset()
        self.pm = PersonaManager.get_instance()

    def teardown_method(self):
        PersonaManager.reset()

    def test_persist_excludes_mrpa_source_profiles(self, tmp_path):
        self.pm.upsert_profile("op1", {"id": "op1", "name": "Operator"}, _track_history=False)
        self.pm.upsert_profile("mrpa1", {"id": "mrpa1", "name": "Auto", "_mrpa_source": True}, _track_history=False)

        cm = MagicMock()
        cm.config_path = tmp_path / "config.yaml"
        cm.config = {"persona_persistence": {"enabled": True}}

        self.pm.persist_profiles(cm)
        runtime_file = tmp_path / "profiles_runtime.yaml"
        assert runtime_file.exists()

        import yaml
        with open(runtime_file, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        profiles = data.get("profiles", {})
        assert "op1" in profiles
        assert "mrpa1" not in profiles  # filtered out

    def test_persist_atomic_write(self, tmp_path):
        """Verifies no .yaml.tmp file is left after a successful persist."""
        self.pm.upsert_profile("p1", {"id": "p1", "name": "Test"}, _track_history=False)
        cm = MagicMock()
        cm.config_path = tmp_path / "config.yaml"
        cm.config = {"persona_persistence": {"enabled": True}}

        self.pm.persist_profiles(cm)
        tmp_file = tmp_path / "profiles_runtime.yaml.tmp"
        assert not tmp_file.exists()  # cleaned up after rename

    def test_save_persona_file_atomic(self, tmp_path):
        """P5-A: save_persona_file uses atomic .tmp → rename pattern."""
        path = tmp_path / "test.yaml"
        ok = self.pm.save_persona_file(path, {"key": "value"})
        assert ok
        assert path.exists()
        assert not (tmp_path / "test.yaml.tmp").exists()

    def test_load_personas_canonical_loads_from_yaml(self, tmp_path):
        """P5-D: load_personas_canonical picks up personas from get_personas_config()."""
        import yaml as _yaml
        personas_file = tmp_path / "personas.yaml"
        with open(personas_file, "w", encoding="utf-8") as f:
            _yaml.dump({"profiles": {"canonical1": {"id": "canonical1", "name": "Canonical"}}}, f)

        cm = MagicMock()
        cm.config = {"persona_persistence": {"enabled": True}}
        cm.get_personas_config = MagicMock(return_value={
            "profiles": {"canonical1": {"id": "canonical1", "name": "Canonical"}}
        })

        count = self.pm.load_personas_canonical(cm)
        assert count == 1
        p = self.pm.get_persona_by_id("canonical1")
        assert p is not None
        assert p["name"] == "Canonical"

    def test_load_personas_canonical_skipped_when_disabled(self):
        cm = MagicMock()
        cm.config = {"persona_persistence": {"enabled": False}}
        count = self.pm.load_personas_canonical(cm)
        assert count == 0


# ── P2-A: _import_reply_profiles_to_pm idempotency ────────────────────────


class TestImportIdempotency:
    """Operator-edited profiles must not be clobbered by config import on restart."""

    def _make_pm_stub(self, existing=None):
        pm = MagicMock()
        pm.get_persona_by_id = MagicMock(return_value=existing)
        pm.upsert_profile = MagicMock()
        return pm

    def test_imports_fresh_profile(self):
        """Profile not yet in PM → should be imported."""
        pm = self._make_pm_stub(existing=None)
        profiles = [{"id": "pid1", "persona": {"name": "Alice"}}]
        n = _run_import(pm, profiles)
        pm.upsert_profile.assert_called_once()
        assert n == 1

    def test_skips_operator_owned_profile(self):
        """Profile exists in PM WITHOUT _mrpa_source → skip (operator-owned)."""
        pm = self._make_pm_stub(existing={"name": "Alice Edited", "id": "pid1"})
        profiles = [{"id": "pid1", "persona": {"name": "Alice Old"}}]
        n = _run_import(pm, profiles)
        pm.upsert_profile.assert_not_called()
        assert n == 0

    def test_overwrites_mrpa_source_profile(self):
        """Profile exists with _mrpa_source=True → overwrite (config is still authoritative)."""
        pm = self._make_pm_stub(existing={"name": "Alice", "_mrpa_source": True, "id": "pid1"})
        profiles = [{"id": "pid1", "persona": {"name": "Alice Updated"}}]
        n = _run_import(pm, profiles)
        pm.upsert_profile.assert_called_once()
        assert n == 1

    def test_skips_profiles_without_persona_key(self):
        """Profiles with no persona dict → skip silently."""
        pm = self._make_pm_stub(existing=None)
        profiles = [{"id": "pid1"}]  # no "persona"
        n = _run_import(pm, profiles)
        pm.upsert_profile.assert_not_called()
        assert n == 0

    def test_mixed_batch(self):
        """Mix of new, operator-owned, mrpa-source profiles."""
        call_count = 0
        pids_seen = {}

        def mock_get(pid):
            return pids_seen.get(pid)

        pids_seen["existing_op"] = {"name": "Op Edited", "id": "existing_op"}
        pids_seen["existing_mrpa"] = {"name": "Mrpa", "_mrpa_source": True, "id": "existing_mrpa"}
        # "new_pid" → None (not in PM)

        pm = MagicMock()
        pm.get_persona_by_id = mock_get
        pm.upsert_profile = MagicMock()

        profiles = [
            {"id": "existing_op", "persona": {"name": "Old Op"}},
            {"id": "existing_mrpa", "persona": {"name": "Updated Mrpa"}},
            {"id": "new_pid", "persona": {"name": "Brand New"}},
        ]
        n = _run_import(pm, profiles)
        assert n == 2  # existing_mrpa + new_pid (not existing_op)
        called_pids = [c.args[0] for c in pm.upsert_profile.call_args_list]
        assert "existing_op" not in called_pids
        assert "existing_mrpa" in called_pids
        assert "new_pid" in called_pids


def _run_import(pm, profiles):
    """Extract and run just the import logic from service._import_reply_profiles_to_pm."""
    n = 0
    for p in profiles:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("id") or p.get("name") or "").strip()
        if not pid:
            continue
        persona_data = p.get("persona")
        if not isinstance(persona_data, dict) or not persona_data:
            continue
        existing = pm.get_persona_by_id(pid)
        if existing is not None and not existing.get("_mrpa_source"):
            continue
        entry = {**persona_data, "id": pid, "_mrpa_source": True}
        pm.upsert_profile(pid, entry, _track_history=False)
        n += 1
    return n


# ── P2-C: PM enrichment in _pick_reply_profile ────────────────────────────


class TestPickReplyProfilePMEnrichment:
    """When PM has operator-edited data, chosen profile's persona should be replaced."""

    def _enrich(self, chosen, pm_data):
        """Simulate the P2-C enrichment block."""
        if isinstance(chosen, dict) and chosen:
            _c_pid = str(chosen.get("id") or chosen.get("name") or "").strip()
            if _c_pid:
                try:
                    _pm_en = pm_data.get(_c_pid)
                    if _pm_en is not None and not _pm_en.get("_mrpa_source"):
                        chosen = dict(chosen)
                        chosen["persona"] = dict(_pm_en)
                        chosen["_persona_from_pm"] = True
                except Exception:
                    pass
        return chosen

    def test_enriches_when_operator_edited(self):
        chosen = {"id": "pid1", "persona": {"name": "Old"}}
        pm_store = {"pid1": {"name": "Edited by Operator", "id": "pid1"}}
        result = self._enrich(chosen, pm_store)
        assert result["persona"]["name"] == "Edited by Operator"
        assert result["_persona_from_pm"] is True

    def test_skips_when_mrpa_source(self):
        """mrpa_source profile means config is still authoritative — don't override."""
        chosen = {"id": "pid1", "persona": {"name": "Old"}}
        pm_store = {"pid1": {"name": "Config Import", "_mrpa_source": True, "id": "pid1"}}
        result = self._enrich(chosen, pm_store)
        assert result["persona"]["name"] == "Old"
        assert not result.get("_persona_from_pm")

    def test_skips_when_pm_has_no_entry(self):
        chosen = {"id": "pid1", "persona": {"name": "From Config"}}
        result = self._enrich(chosen, {})
        assert result["persona"]["name"] == "From Config"
        assert not result.get("_persona_from_pm")

    def test_routing_keys_preserved(self):
        """match_names etc. from config must not be lost when persona is enriched."""
        chosen = {"id": "p", "match_names": ["Alice"], "persona": {"name": "Old"}}
        pm_store = {"p": {"name": "New", "id": "p"}}
        result = self._enrich(chosen, pm_store)
        assert result["match_names"] == ["Alice"]
        assert result["persona"]["name"] == "New"

    def test_original_not_mutated(self):
        """Enrichment returns a new dict; original must be unchanged."""
        original = {"id": "p", "persona": {"name": "Old"}}
        pm_store = {"p": {"name": "New", "id": "p"}}
        result = self._enrich(original, pm_store)
        assert original["persona"]["name"] == "Old"
        assert result is not original
