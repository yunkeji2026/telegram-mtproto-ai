"""Unit tests for src.skills.relationship_stager.RelationshipStager (P3 persona routing)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from src.skills.relationship_stager import RelationshipStager, _STAGE_ORDER


class TestScoreToStage:
    def test_initial_low(self):
        s = RelationshipStager({})
        assert s.score_to_stage(0) == "initial"
        assert s.score_to_stage(24.9) == "initial"

    def test_warming(self):
        s = RelationshipStager({})
        assert s.score_to_stage(25) == "warming"
        assert s.score_to_stage(54.9) == "warming"

    def test_intimate(self):
        s = RelationshipStager({})
        assert s.score_to_stage(55) == "intimate"
        assert s.score_to_stage(79.9) == "intimate"

    def test_steady(self):
        s = RelationshipStager({})
        assert s.score_to_stage(80) == "steady"
        assert s.score_to_stage(100) == "steady"

    def test_custom_bands(self):
        s = RelationshipStager({}, bands={"to_warming": 30, "to_intimate": 60, "to_steady": 90})
        assert s.score_to_stage(29) == "initial"
        assert s.score_to_stage(30) == "warming"
        assert s.score_to_stage(60) == "intimate"
        assert s.score_to_stage(90) == "steady"

    def test_invalid_score_returns_initial(self):
        s = RelationshipStager({})
        assert s.score_to_stage(float("nan")) == "initial"


class TestResolve:
    def test_returns_none_when_disabled(self):
        s = RelationshipStager({"initial": "pid1"}, enabled=False)
        assert s.resolve(10) is None

    def test_returns_none_when_no_map(self):
        s = RelationshipStager({})
        assert s.resolve(50) is None

    def test_returns_none_when_score_is_none(self):
        s = RelationshipStager({"initial": "pid1"})
        assert s.resolve(None) is None

    def test_exact_stage_match(self):
        s = RelationshipStager({"initial": "pid_cold", "warming": "pid_warm"})
        assert s.resolve(10) == "pid_cold"
        assert s.resolve(40) == "pid_warm"

    def test_fallback_down_missing_stage(self):
        # steady missing — fallback down to intimate
        s = RelationshipStager({"initial": "p1", "intimate": "p3"}, fallback_up=False)
        assert s.resolve(90) == "p3"  # steady → fallback down to intimate

    def test_fallback_up_missing_stage(self):
        # initial missing — fallback up to warming
        s = RelationshipStager({"warming": "p_warm", "steady": "p_steady"}, fallback_up=True)
        assert s.resolve(5) == "p_warm"  # initial → fallback up to warming

    def test_no_fallback_possible_returns_none(self):
        # Only steady mapped; score=10 (initial), no fallback up possible (fallback_up=True)
        s = RelationshipStager({"steady": "pid"}, fallback_up=True)
        # Score 10 is initial. Fallback up tries warming, intimate, steady → finds "pid"
        assert s.resolve(10) == "pid"

    def test_empty_value_in_map_ignored(self):
        s = RelationshipStager({"initial": "", "warming": "p_warm"})
        assert s.resolve(30) == "p_warm"
        # Initial has empty value — no match; no fallback configured → None
        assert s.resolve(10) is None


class TestFromConfig:
    def test_empty_config(self):
        s = RelationshipStager.from_config({})
        assert not s.enabled
        assert s.resolve(50) is None

    def test_auto_enabled_when_map_set(self):
        s = RelationshipStager.from_config({"stage_persona_ids": {"initial": "pid"}})
        assert s.enabled

    def test_explicit_disable(self):
        s = RelationshipStager.from_config({
            "stage_persona_ids": {"initial": "pid"},
            "stage_persona_enabled": False,
        })
        assert not s.enabled
        assert s.resolve(10) is None

    def test_custom_bands_from_config(self):
        s = RelationshipStager.from_config({
            "stage_persona_ids": {"initial": "p", "warming": "q"},
            "stage_persona_bands": {"to_warming": 40},
        })
        assert s.score_to_stage(39) == "initial"
        assert s.score_to_stage(40) == "warming"

    def test_fallback_up_from_config(self):
        s = RelationshipStager.from_config({
            "stage_persona_ids": {"steady": "top"},
            "stage_persona_fallback_up": True,
        })
        assert s.resolve(5) == "top"  # initial → fallback up all the way to steady

    def test_invalid_config_type(self):
        s = RelationshipStager.from_config(None)
        assert not s.enabled

    def test_summary_structure(self):
        s = RelationshipStager.from_config({
            "stage_persona_ids": {"warming": "pw"},
        })
        d = s.summary()
        assert "enabled" in d
        assert "stage_map" in d
        assert "bands" in d
        assert d["stage_map"]["warming"] == "pw"
