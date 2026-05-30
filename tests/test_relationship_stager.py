"""W3-3M RelationshipStager 单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from src.contacts.relationship_stager import stage_directive, _intim_band


# ── _intim_band ───────────────────────────────────────────

class TestIntimBand:
    def test_soulmate(self):
        assert _intim_band(85) == "soulmate"
        assert _intim_band(80.0) == "soulmate"

    def test_close(self):
        assert _intim_band(55) == "close"
        assert _intim_band(79.9) == "close"

    def test_friend(self):
        assert _intim_band(25) == "friend"
        assert _intim_band(54.9) == "friend"

    def test_stranger(self):
        assert _intim_band(0) == "stranger"
        assert _intim_band(24.9) == "stranger"

    def test_none_returns_unknown(self):
        assert _intim_band(None) == "unknown"

    def test_invalid_returns_unknown(self):
        assert _intim_band("bad") == "unknown"  # type: ignore


# ── stage_directive ───────────────────────────────────────

class TestStageDirective:
    def test_initial_returns_non_empty(self):
        d = stage_directive("INITIAL")
        assert d
        assert "【关系阶段】" in d

    def test_initial_lowercase_normalized(self):
        d = stage_directive("initial")
        assert d
        assert "【关系阶段】" in d

    def test_none_stage_treated_as_initial(self):
        d = stage_directive(None)
        assert d
        assert "【关系阶段】" in d

    def test_lost_stages_produce_directive(self):
        for stage in ("LOST_HANDOFF", "LOST_LINE_SILENT"):
            d = stage_directive(stage)
            assert d, f"{stage} should produce a directive"
            assert "沉默" in d or "联系" in d or "久" in d

    def test_bonded_soulmate_friendlier_than_initial(self):
        d_bonded = stage_directive("BONDED", intimacy_score=85)
        d_initial = stage_directive("INITIAL", intimacy_score=10)
        assert "朋友" in d_bonded or "深厚" in d_bonded or "轻松" in d_bonded
        assert "首次" in d_initial or "新用户" in d_initial

    def test_building_high_intimacy(self):
        d = stage_directive("LINE_ENGAGED", intimacy_score=70)
        assert "【关系阶段】" in d
        assert "快速升温" in d or "亲密度" in d or "升温" in d

    def test_building_low_intimacy(self):
        d = stage_directive("LINE_ACCEPTED", intimacy_score=20)
        assert "【关系阶段】" in d

    def test_warm_up_stage(self):
        d = stage_directive("HANDOFF_SENT")
        assert "【关系阶段】" in d
        assert "观望" in d or "初建" in d

    def test_needs_manual_merge_returns_empty(self):
        d = stage_directive("NEEDS_MANUAL_MERGE")
        assert d == ""

    def test_unknown_stage_returns_empty(self):
        d = stage_directive("TOTALLY_UNKNOWN_STAGE_XYZ")
        assert d == ""

    def test_converted_returns_directive(self):
        d = stage_directive("CONVERTED", intimacy_score=90)
        assert d
        assert "【关系阶段】" in d

    def test_combo_key_sorted_consistency(self):
        """同 stage 不同 score 档会产出不同文本。"""
        d_low = stage_directive("BONDED", intimacy_score=10)
        d_high = stage_directive("BONDED", intimacy_score=90)
        # 两者都应有指令，但高分应更亲切
        assert d_low
        assert d_high
        assert d_high != d_low


# ── rpa_hooks 协议完整性 ──────────────────────────────────

class TestRpaHooksProtocol:
    """NoopContactHooks 必须实现 get_journey_funnel_stage。"""

    def test_noop_has_method(self):
        from src.contacts.rpa_hooks import NoopContactHooks
        h = NoopContactHooks()
        result = h.get_journey_funnel_stage(
            channel="line", account_id="a", external_id="e",
        )
        assert result is None

    def test_gateway_hooks_has_method(self):
        from src.contacts.rpa_hooks import GatewayContactHooks
        assert hasattr(GatewayContactHooks, "get_journey_funnel_stage")

    def test_gateway_hooks_returns_none_when_no_ci(self, tmp_path):
        from src.contacts.store import ContactStore
        from src.contacts.handoff import HandoffTokenService
        from src.contacts.merge import MergeService
        from src.contacts.gateway import ContactGateway
        from src.contacts.rpa_hooks import GatewayContactHooks

        store = ContactStore(db_path=tmp_path / "c.db")
        svc = HandoffTokenService(store)
        merge = MergeService(store)
        gw = ContactGateway(store, svc, merge)
        hooks = GatewayContactHooks(gw)
        result = hooks.get_journey_funnel_stage(
            channel="line", account_id="acc", external_id="nobody",
        )
        assert result is None

    def test_gateway_hooks_returns_stage_after_on_peer_seen(self, tmp_path):
        from src.contacts.store import ContactStore
        from src.contacts.handoff import HandoffTokenService
        from src.contacts.merge import MergeService
        from src.contacts.gateway import ContactGateway
        from src.contacts.rpa_hooks import GatewayContactHooks

        store = ContactStore(db_path=tmp_path / "c.db")
        svc = HandoffTokenService(store)
        merge = MergeService(store)
        gw = ContactGateway(store, svc, merge)
        hooks = GatewayContactHooks(gw)

        hooks.on_peer_seen(channel="line", account_id="acc", external_id="u1")
        result = hooks.get_journey_funnel_stage(
            channel="line", account_id="acc", external_id="u1",
        )
        # 新建 journey 的默认 stage 是 INITIAL
        assert result == "INITIAL"
