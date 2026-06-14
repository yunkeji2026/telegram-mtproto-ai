"""AssignmentService（坐席自动派单建议）单元测试。

锁定纯逻辑契约：在线筛选、最少负载选择、确定性 tiebreak、负载上限、
批量轮转、已认领跳过、默认关闭，以及配置解析与归一化。
"""

from src.workspace.assignment import (
    AssignmentService,
    parse_auto_assign_config,
    DEFAULTS,
)


def _p(agent_id, status="online", name=""):
    return {"agent_id": agent_id, "status": status, "display_name": name or agent_id}


def _claim(cid, agent_id):
    return {"conversation_id": cid, "agent_id": agent_id}


# ── 配置解析 ──────────────────────────────────────────────

def test_parse_defaults_when_missing():
    cfg = parse_auto_assign_config({})
    assert cfg["enabled"] is False
    assert cfg["strategy"] == "least_loaded"
    assert cfg["online_only"] is True
    assert cfg["max_claims_per_agent"] == 0


def test_parse_reads_workspace_auto_assign():
    cfg = parse_auto_assign_config({
        "workspace": {"auto_assign": {
            "enabled": True, "strategy": "round_robin",
            "online_only": False, "max_claims_per_agent": 5,
        }}
    })
    assert cfg["enabled"] is True
    assert cfg["strategy"] == "round_robin"
    assert cfg["online_only"] is False
    assert cfg["max_claims_per_agent"] == 5


def test_parse_invalid_strategy_falls_back():
    cfg = parse_auto_assign_config({"workspace": {"auto_assign": {"strategy": "nonsense"}}})
    assert cfg["strategy"] == "least_loaded"


def test_parse_negative_cap_clamped():
    cfg = parse_auto_assign_config({"workspace": {"auto_assign": {"max_claims_per_agent": -3}}})
    assert cfg["max_claims_per_agent"] == 0


# ── eligible_agents ──────────────────────────────────────

def test_eligible_excludes_offline():
    svc = AssignmentService({"enabled": True})
    out = svc.eligible_agents([_p("a1", "online"), _p("a2", "offline")])
    assert [a["agent_id"] for a in out] == ["a1"]


def test_eligible_online_only_excludes_busy():
    svc = AssignmentService({"enabled": True, "online_only": True})
    out = svc.eligible_agents([_p("a1", "online"), _p("a2", "busy")])
    assert [a["agent_id"] for a in out] == ["a1"]


def test_eligible_allows_busy_when_not_online_only():
    svc = AssignmentService({"enabled": True, "online_only": False})
    out = svc.eligible_agents([_p("a1", "online"), _p("a2", "busy")])
    assert {a["agent_id"] for a in out} == {"a1", "a2"}


def test_eligible_skips_blank_agent_id():
    svc = AssignmentService({"enabled": True})
    out = svc.eligible_agents([{"agent_id": "", "status": "online"}, _p("a1")])
    assert [a["agent_id"] for a in out] == ["a1"]


# ── suggest（单会话）──────────────────────────────────────

def test_suggest_none_when_no_agents():
    svc = AssignmentService({"enabled": True})
    assert svc.suggest(presence=[], claims=[]) is None


def test_suggest_picks_least_loaded():
    svc = AssignmentService({"enabled": True})
    presence = [_p("a1"), _p("a2")]
    claims = [_claim("c1", "a1"), _claim("c2", "a1")]  # a1 负载 2，a2 负载 0
    sug = svc.suggest(presence=presence, claims=claims)
    assert sug["agent_id"] == "a2"
    assert sug["load"] == 0


def test_suggest_tiebreak_by_agent_id():
    svc = AssignmentService({"enabled": True})
    presence = [_p("b2"), _p("a1")]  # 均 0 负载 → agent_id 升序取 a1
    sug = svc.suggest(presence=presence, claims=[])
    assert sug["agent_id"] == "a1"


def test_suggest_respects_cap():
    svc = AssignmentService({"enabled": True, "max_claims_per_agent": 2})
    presence = [_p("a1")]
    claims = [_claim("c1", "a1"), _claim("c2", "a1")]  # a1 已达 cap=2
    assert svc.suggest(presence=presence, claims=claims) is None


def test_suggest_agent_name_falls_back_to_id():
    svc = AssignmentService({"enabled": True})
    sug = svc.suggest(presence=[{"agent_id": "a1", "status": "online"}], claims=[])
    assert sug["agent_name"] == "a1"


# ── suggest_for_chats（批量）──────────────────────────────

def test_for_chats_disabled_returns_empty():
    svc = AssignmentService({"enabled": False})
    out = svc.suggest_for_chats(
        chats=[{"conversation_id": "c1"}], presence=[_p("a1")], claims=[],
    )
    assert out == {}


def test_for_chats_skips_claimed():
    svc = AssignmentService({"enabled": True})
    out = svc.suggest_for_chats(
        chats=[{"conversation_id": "c1"}, {"conversation_id": "c2"}],
        presence=[_p("a1")],
        claims=[_claim("c1", "a1")],  # c1 已认领 → 跳过
    )
    assert "c1" not in out
    assert "c2" in out


def test_for_chats_round_robins_within_batch():
    svc = AssignmentService({"enabled": True})
    chats = [{"conversation_id": f"c{i}"} for i in range(4)]
    presence = [_p("a1"), _p("a2")]
    out = svc.suggest_for_chats(chats=chats, presence=presence, claims=[])
    assigned = [out[c["conversation_id"]]["agent_id"] for c in chats]
    # 4 个会话两名空闲坐席 → 各 2 个，不会全挤一人
    assert assigned.count("a1") == 2
    assert assigned.count("a2") == 2


def test_for_chats_no_online_agents_returns_empty():
    svc = AssignmentService({"enabled": True})
    out = svc.suggest_for_chats(
        chats=[{"conversation_id": "c1"}],
        presence=[_p("a1", "offline")],
        claims=[],
    )
    assert out == {}


def test_from_config_helper():
    svc = AssignmentService.from_config({"workspace": {"auto_assign": {"enabled": True}}})
    assert svc.enabled is True
    assert svc.cfg["strategy"] == DEFAULTS["strategy"]
