"""Phase ④续³ 关系/成长面板指令接线测试（轻量绑定，免全量 init）。

校验：触发词短路、非陪伴域不劫持、等级/进度透出、剧情足迹（经历过/可玩/待解锁）、
等级解锁预览、空信号温和兜底。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_SMcls = __import__(
    "src.skills.skill_manager", fromlist=["SkillManager"]
).SkillManager

_SCENARIOS = {
    "coffee_date": {
        "title": "初次咖啡约会",
        "min_bond_level": 2,
        "beats": [{"id": "a", "directive": "x"}],
    },
    "starry_night": {
        "title": "星空下的约定",
        "min_bond_level": 3,
        "require_unlock": "all_story",
        "beats": [{"id": "b", "directive": "y"}],
    },
}


class _SM:
    _handle_growth_command = _SMcls._handle_growth_command
    _GROWTH_TRIGGERS = _SMcls._GROWTH_TRIGGERS
    _progress_bar = staticmethod(_SMcls._progress_bar)
    _effective_intimacy = _SMcls._effective_intimacy
    _bond_level_from_context = _SMcls._bond_level_from_context
    _story_cfg = _SMcls._story_cfg
    _story_scenarios = _SMcls._story_scenarios
    _story_outcomes = _SMcls._story_outcomes

    def __init__(self, *, companion=True, story=True, domain="conversion",
                 unlocks=None):
        comp = {
            "enabled": companion,
            "story": {"enabled": story, "scenarios": _SCENARIOS},
        }
        if unlocks is not None:
            comp["bond_level"] = {"enabled": True, "unlocks": unlocks}
        cfg = {"domain": domain, "companion": comp}
        self.config = SimpleNamespace(config=cfg)
        self.logger = logging.getLogger("test_growth")


def _ctx(intimacy=None, entitlement=None):
    c = {}
    if intimacy is not None:
        c["intimacy_score"] = intimacy
    if entitlement is not None:
        c["entitlement"] = entitlement
    return c


def test_non_trigger_passthrough():
    sm = _SM()
    assert sm._handle_growth_command("今天天气不错", _ctx(intimacy=60), "c") is None


def test_non_companion_domain_not_hijacked():
    sm = _SM(companion=False)
    assert sm._handle_growth_command("我们的关系", _ctx(intimacy=60), "c") is None


def test_panel_shows_level_and_progress():
    sm = _SM()
    out = sm._handle_growth_command("我们的关系", _ctx(intimacy=60), "c")
    assert out and "💞" in out
    # 进度条字符出现
    assert ("▮" in out) or ("▯" in out)


def test_panel_lists_available_and_locked_stories():
    sm = _SM()
    # intimacy 60 → level 3：coffee 可玩；starry 需 all_story → 待解锁
    out = sm._handle_growth_command("成长", _ctx(intimacy=60), "c")
    assert "还能一起经历" in out and "初次咖啡约会" in out
    assert "🔒" in out and "星空下的约定" in out


def test_panel_marks_completed_stories():
    sm = _SM()
    ctx = _ctx(intimacy=60)
    from src.utils.companion_relationship import get_rel_state
    get_rel_state(ctx, "c")["story_done"] = ["coffee_date"]
    out = sm._handle_growth_command("我们的故事", ctx, "c")
    assert "一起经历过" in out and "《初次咖啡约会》" in out
    # 已完成的不再出现在「还能一起经历」
    assert "还能一起经历：《初次咖啡约会》" not in out


def test_panel_low_intimacy_gentle():
    sm = _SM(story=False)
    out = sm._handle_growth_command("我的等级", _ctx(), "c")
    assert out and "刚认识" in out


def test_panel_shows_unlocks():
    sm = _SM(unlocks={"intimate": ["exclusive_album"]})
    out = sm._handle_growth_command("我们的关系", _ctx(intimacy=60), "c")
    assert "已解锁" in out and "exclusive_album" in out


def test_progress_bar_bounds():
    assert _SM._progress_bar(0.0) == "▯" * 10
    assert _SM._progress_bar(1.0) == "▮" * 10
    assert _SM._progress_bar(0.5).count("▮") == 5
