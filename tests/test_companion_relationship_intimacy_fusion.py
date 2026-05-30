"""W2-D1：companion_relationship × IntimacyEngine 融合单测。

覆盖：
  - derive_stage_from_intimacy 阈值边界
  - fuse_with_intimacy 的「永远只降不升」+「新用户保护」
  - reunion 检测（长沉默后回归）
  - build_relationship_prompt_block 向后兼容（intimacy_score=None 等同旧版）
"""
import pytest

from src.utils.companion_relationship import (
    INTIMACY_BAND_DEFAULTS,
    STAGE_ORDER,
    build_relationship_prompt_block,
    derive_stage_from_intimacy,
    fuse_with_intimacy,
)


# 共享 cfg：thresholds 和老测试一致，保证可复现
_CFG_BASE = {
    "enabled": True,
    "thresholds": {
        "initial_to_warming_exchanges": 4,
        "warming_to_intimate_exchanges": 14,
        "intimate_to_steady_exchanges": 35,
    },
    "intimacy_fusion": {"enabled": True},
}


class TestDeriveStageFromIntimacy:
    def test_none_returns_none(self):
        assert derive_stage_from_intimacy(None) is None

    def test_invalid_returns_none(self):
        assert derive_stage_from_intimacy("not-a-number") is None

    @pytest.mark.parametrize("score,expected", [
        (0.0, "initial"),
        (24.9, "initial"),
        (25.0, "warming"),
        (54.9, "warming"),
        (55.0, "intimate"),
        (79.9, "intimate"),
        (80.0, "steady"),
        (100.0, "steady"),
    ])
    def test_default_bands(self, score, expected):
        assert derive_stage_from_intimacy(score) == expected

    def test_custom_bands(self):
        bands = {"to_warming": 10, "to_intimate": 30, "to_steady": 60}
        assert derive_stage_from_intimacy(15, bands) == "warming"
        assert derive_stage_from_intimacy(35, bands) == "intimate"
        assert derive_stage_from_intimacy(70, bands) == "steady"


class TestFuseWithIntimacy:
    def test_no_score_passthrough(self):
        """intimacy_score=None → fusion 不生效，effective=raw。"""
        eff, reunion = fuse_with_intimacy("steady", 50, None, _CFG_BASE)
        assert eff == "steady"
        assert reunion is False

    def test_fusion_disabled_passthrough(self):
        cfg = {**_CFG_BASE, "intimacy_fusion": {"enabled": False}}
        eff, reunion = fuse_with_intimacy("steady", 50, 5.0, cfg)
        assert eff == "steady"
        assert reunion is False

    def test_new_user_protected(self):
        """exchange_count 未过 warming 阈值 → 不让 intimacy 降级。"""
        # raw=initial(实际), exchange_count=2(<4 阈值), score=0
        # 即使 intim_stage=initial 与 raw=initial 相同也无降阶
        eff, reunion = fuse_with_intimacy("initial", 2, 5.0, _CFG_BASE)
        assert eff == "initial"
        assert reunion is False

    def test_silence_decay_degrades_steady_to_initial(self):
        """50 轮 steady 但 score 衰减到 8 → effective=initial + reunion=True。"""
        eff, reunion = fuse_with_intimacy("steady", 50, 8.0, _CFG_BASE)
        assert eff == "initial"
        assert reunion is True

    def test_silence_decay_degrades_steady_to_warming(self):
        """50 轮 steady 但 score=30 → effective=warming + reunion=True。"""
        eff, reunion = fuse_with_intimacy("steady", 50, 30.0, _CFG_BASE)
        assert eff == "warming"
        assert reunion is True

    def test_aligned_no_reunion(self):
        """raw=intimate + score=70（intimate 区间）→ effective=intimate, no reunion。"""
        eff, reunion = fuse_with_intimacy("intimate", 20, 70.0, _CFG_BASE)
        assert eff == "intimate"
        assert reunion is False

    def test_intimacy_higher_than_raw_capped(self):
        """raw=warming + score=85（steady 区间）→ effective=warming（不上调）。"""
        eff, reunion = fuse_with_intimacy("warming", 5, 85.0, _CFG_BASE)
        assert eff == "warming"
        assert reunion is False

    def test_invalid_raw_treated_as_initial(self):
        eff, _ = fuse_with_intimacy("garbage", 20, 50.0, _CFG_BASE)
        assert eff == "initial"


class TestBuildRelationshipPromptBlock:
    def test_backward_compat_no_intimacy(self):
        """不传 intimacy_score → 行为完全等同旧版（不含 reunion 提示）。"""
        st = {"stage": "steady", "exchange_count": 50}
        block = build_relationship_prompt_block(st, _CFG_BASE)
        assert "稳定陪伴" in block
        assert "降级" not in block
        assert "久违" not in block

    def test_reunion_prompt_emitted_on_silence_decay(self):
        st = {"stage": "steady", "exchange_count": 50}
        block = build_relationship_prompt_block(
            st, _CFG_BASE, intimacy_score=8.0,
        )
        # 应降级到 initial 并加 reunion 提示
        assert "初识" in block
        assert "降级" in block or "久违" in block or "重逢" in block
        # 提示对方很久没找你，要先问候
        assert "最近怎么样" in block or "自然问候" in block
        # raw 阶段也应出现以供对照
        assert "稳定陪伴" in block

    def test_aligned_no_reunion_prompt(self):
        st = {"stage": "intimate", "exchange_count": 20}
        block = build_relationship_prompt_block(
            st, _CFG_BASE, intimacy_score=70.0,
        )
        assert "暧昧陪伴" in block
        assert "降级" not in block
        assert "重逢" not in block

    def test_disabled_companion_returns_empty(self):
        st = {"stage": "steady", "exchange_count": 50}
        block = build_relationship_prompt_block(
            {"enabled": False}, _CFG_BASE,  # 错位：传递空 cfg
        ) if False else build_relationship_prompt_block(
            st, {**_CFG_BASE, "enabled": False}, intimacy_score=8.0,
        )
        assert block == ""

    def test_score_str_in_reunion(self):
        st = {"stage": "intimate", "exchange_count": 20}
        block = build_relationship_prompt_block(
            st, _CFG_BASE, intimacy_score=12.4,
        )
        assert "12/100" in block  # 整数化展示


def test_intimacy_band_defaults_align_with_ai_studio():
    """阈值文档化：与 ai_studio.html 关系看板的 4 段保持同步。"""
    assert INTIMACY_BAND_DEFAULTS["to_warming"] == 25.0
    assert INTIMACY_BAND_DEFAULTS["to_intimate"] == 55.0
    assert INTIMACY_BAND_DEFAULTS["to_steady"] == 80.0


def test_stage_order_unchanged():
    """新增功能不能改 STAGE_ORDER（其它模块依赖）。"""
    assert STAGE_ORDER == ("initial", "warming", "intimate", "steady")
