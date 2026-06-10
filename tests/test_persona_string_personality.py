"""人设 schema 漂移健壮性回归。

背景（2026-06 桌面端实测发现）：profiles_runtime.yaml 里 zhang_jingguang 的
``personality`` 是一段**字符串**（自由格式人设），而格式化器假设它是 dict，
``persona.get("personality", {}).get("traits")`` 对字符串抛 AttributeError；
该异常在 ai_client 中被 except 静默吞掉 → 整段人设丢失、悄悄回落域默认，
造成「徽标显示张景光，回复却是娇嗲女声」的信任崩塌级故障。

本测试锁死：
1. normalize_profile_shape 把字符串 personality 收敛为 {'style': ...}，其余子结构兜底成 dict；
2. 字符串 personality 的人设过 build_system_prompt 不再崩溃，且其内容真进系统提示；
3. upsert/account 解析后该人设可被 get_persona_with_tier 正常取出。
"""
import pytest
from src.utils.persona_manager import PersonaManager


def _pm() -> PersonaManager:
    PersonaManager.reset()
    return PersonaManager.get_instance()


_STRING_PERSONALITY = "成熟风趣，语速适中，善用工程学比喻，绝不使用客服腔调。"


def test_normalize_string_personality_to_dict():
    out = PersonaManager.normalize_profile_shape(
        {"name": "张景光", "role": "工程师", "personality": _STRING_PERSONALITY}
    )
    assert isinstance(out["personality"], dict)
    assert out["personality"]["style"] == _STRING_PERSONALITY


def test_normalize_coerces_non_dict_subfields():
    out = PersonaManager.normalize_profile_shape(
        {"name": "x", "personality": None, "speaking": None,
         "identity": "bad", "boundaries": [1, 2], "context": 3}
    )
    for k in ("personality", "speaking", "identity", "boundaries", "context"):
        assert isinstance(out[k], dict)


def test_normalize_passthrough_dict_personality():
    src = {"name": "lin", "personality": {"traits": ["活泼"], "style": "轻松"}}
    out = PersonaManager.normalize_profile_shape(src)
    assert out["personality"]["traits"] == ["活泼"]
    assert out["personality"]["style"] == "轻松"


def test_build_system_prompt_string_personality_no_crash():
    pm = _pm()
    pm.upsert_profile(
        "zhang", {"name": "张景光", "role": "高级电气工程师",
                  "personality": _STRING_PERSONALITY,
                  "identity": {"deny_ai": True, "claim_human": True}}
    )
    # 不应抛异常（修复前会 AttributeError）
    prompt = pm.build_system_prompt(account_persona_id="zhang")
    assert isinstance(prompt, str) and prompt
    # 人设内容真进了提示（名字 + 字符串风味）
    assert "张景光" in prompt
    assert "成熟风趣" in prompt


def test_account_persona_resolves_after_normalize():
    pm = _pm()
    pm.upsert_profile("zhang", {"id": "zhang", "name": "张景光",
                                "personality": _STRING_PERSONALITY})
    p, tier = pm.get_persona_with_tier("no_binding_chat", "zhang")
    assert tier == PersonaManager._TIER_ACCOUNT
    assert p["name"] == "张景光"
    assert isinstance(p["personality"], dict)
