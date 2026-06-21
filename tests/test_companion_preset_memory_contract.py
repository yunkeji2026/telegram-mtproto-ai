"""预设契约测试：companion.yaml 必须用「代码真正读取的键名」激活长期记忆深化栈。

背景（代码实况勘探发现）：episodic 记忆引擎（store + skill_manager 接线 + 巩固/
矛盾消解/来源分级 + proactive_topic 主动开场）早已全量写好，但 companion 预设里
``memory.salience`` 拼错了键（代码读 ``memory.salience_rerank``），导致情绪显著性
重排这条护城河特性被「配置在、代码读不到」地静默关掉；且 ``companion.proactive_topic``
未开 → 记忆驱动的主动惦记开场也未激活。

本测试把「预设激活意图」与「代码真实读取口径」绑定，任一侧再漂移即红——
直接针对 AGENTS.md 记录的「文档/配置落后于代码」教训。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PRESET = ROOT / "config" / "presets" / "companion.yaml"


@pytest.fixture(scope="module")
def preset() -> dict:
    data = yaml.safe_load(PRESET.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "companion 预设应为 YAML 映射"
    return data


def test_preset_file_exists():
    assert PRESET.exists(), f"缺少陪伴预设: {PRESET}"


def test_memory_core_enabled(preset):
    mem = preset.get("memory") or {}
    assert mem.get("enabled") is True
    assert (mem.get("extract") or {}).get("enabled") is True
    con = mem.get("consolidation") or {}
    assert con.get("enabled") is True, "巩固（晋升 stable）应在陪伴预设默认开"
    # 这三项是「记得住且记得准」的关键：矛盾消解 / 新证据推翻旧结论 / 按来源分级置信
    assert con.get("resolve_contradictions") is True
    assert con.get("supersede_stable") is True
    assert con.get("source_aware") is True


def test_salience_rerank_resolves_enabled_via_code_path(preset):
    """情绪显著性重排必须经「代码真正用的解析器」判定为开——绑定键名口径。"""
    from src.skills.skill_manager import resolve_salience_rerank_cfg

    mem = preset.get("memory") or {}
    scfg = resolve_salience_rerank_cfg(mem)
    assert bool(scfg.get("enabled")) is True, (
        "salience 重排在预设里应激活；若失败多半是键名漂移"
        "（应为 memory.salience_rerank，代码经别名同时兼容 memory.salience）"
    )


def test_salience_resolver_tolerates_both_spellings():
    from src.skills.skill_manager import resolve_salience_rerank_cfg

    assert resolve_salience_rerank_cfg(
        {"salience_rerank": {"enabled": True}}
    ).get("enabled") is True
    # 历史简写仍兼容
    assert resolve_salience_rerank_cfg(
        {"salience": {"enabled": True}}
    ).get("enabled") is True
    # 规范键优先级高于别名
    assert resolve_salience_rerank_cfg(
        {"salience_rerank": {"enabled": False}, "salience": {"enabled": True}}
    ).get("enabled") is False
    assert resolve_salience_rerank_cfg({}) == {}
    assert resolve_salience_rerank_cfg(None) == {}


def test_proactive_topic_enabled(preset):
    """记忆驱动的主动惦记开场（P1/P2）须在陪伴预设激活，且键名与 main.py 读取一致。"""
    comp = preset.get("companion") or {}
    pt = comp.get("proactive_topic") or {}
    assert pt.get("enabled") is True, (
        "companion.proactive_topic.enabled 应为 True"
        "（main._maybe_start_companion_proactive 读取此键）"
    )
    # min_silent_hours 给了就该是正数（避免打扰活跃用户）
    if "min_silent_hours" in pt:
        assert float(pt["min_silent_hours"]) > 0


def test_bond_level_enabled(preset):
    """Phase ②：关系成长系统须在陪伴预设激活（AI 感知关系深度/里程碑）。"""
    comp = preset.get("companion") or {}
    bl = comp.get("bond_level") or {}
    assert bl.get("enabled") is True, "companion.bond_level.enabled 应为 True"
    # unlocks 给了就该是映射，键为合法等级/阶段
    unlocks = bl.get("unlocks")
    if unlocks is not None:
        from src.contacts.relationship_level import level_unlocks
        # 满级应能解出所有配置条目（验证键名合法、可被代码解析）
        all_items = {i for items in unlocks.values() for i in (items or [])}
        assert set(level_unlocks(4, unlocks)) == all_items


def test_story_enabled_and_scenarios_valid(preset):
    """Phase ③：剧情引擎在陪伴预设激活，且场景定义可被引擎正确解析/gate。"""
    from src.skills.story_engine import list_scenarios, start_scenario

    comp = preset.get("companion") or {}
    story = comp.get("story") or {}
    assert story.get("enabled") is True
    scenarios = story.get("scenarios") or {}
    assert scenarios, "陪伴预设应至少配置一个剧情场景"

    # 每个场景须有非空 beats（含 directive）
    for sid, scn in scenarios.items():
        beats = [b for b in (scn.get("beats") or [])
                 if (b.get("directive") or "").strip()]
        assert beats, f"场景 {sid} 无有效 beat"

    # 满权益 + 满关系等级 + 满足全部前置剧情时，所有场景都应可进入（验证 gate 键名合法）。
    # requires_story 的前置以「最易满足结局」喂满（任意 ending 串都给到，warm 也覆盖）。
    full_ent = {"grants": ("all_story",), "unlocked": ("story_ch1",)}
    completed_all = {}
    for scn in scenarios.values():
        for cond in (scn.get("requires_story") or []):
            if isinstance(cond, str):
                completed_all[cond] = ""
            elif isinstance(cond, dict) and cond.get("scenario"):
                completed_all[str(cond["scenario"])] = str(cond.get("ending") or "")
    rows = list_scenarios(
        scenarios, entitlement=full_ent, bond_level=4, completed=completed_all)
    assert rows and all(r["available"] for r in rows)

    # 付费专属场景在「无权益」时应被锁（验证 require_unlock 真生效；前置已喂满以隔离变量）
    paid = [sid for sid, scn in scenarios.items() if scn.get("require_unlock")]
    for sid in paid:
        assert start_scenario(
            sid, scenarios, entitlement={"grants": (), "unlocked": ()},
            bond_level=4, completed=completed_all) is None
