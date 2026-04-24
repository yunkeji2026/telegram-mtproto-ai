"""人工转接：多名客服与 legacy 单客服。"""

from src.utils.human_escalation import (
    HumanEscalationHelper,
    active_teams_status,
    build_telegram_message_link,
    normalize_user_question,
    _format_mentions_line,
    _resolve_agents,
    _select_agents_for_mention,
)


class _FakeStore:
    def __init__(self):
        self.shift = False

    def get_shift_on_duty(self):
        return self.shift

    def cooldown_remaining(self, ck, uk, cd):
        return True, 0.0

    def cooldown_remaining_norm(self, ck, uk, nk, cd):
        return self.cooldown_remaining(ck, uk, cd)

    def mark_escalation(self, ck, uk):
        pass

    def mark_escalation_norm(self, ck, uk, nk):
        self.mark_escalation(ck, uk)

    def reset_repeat_key(self, ck, uk, nk):
        pass

    def record_repeat(self, *a, **k):
        return 3

    def round_robin_next_index(self, n, chat_id=None):
        return 0


def test_resolve_agents_from_list():
    cfg = {
        "human_escalation": {
            "enabled": True,
            "agents": [
                {"user_id": 111, "username": "", "display_name": "A"},
                {"user_id": 0, "username": "bob", "display_name": "B"},
            ],
        }
    }
    h = HumanEscalationHelper(cfg, _FakeStore())
    ag = _resolve_agents(h._cfg())
    assert len(ag) == 2
    assert ag[0]["user_id"] == 111


def test_resolve_agents_legacy():
    cfg = {
        "human_escalation": {
            "human_user_id": 999,
            "human_username": "legacy",
        }
    }
    h = HumanEscalationHelper(cfg, _FakeStore())
    ag = _resolve_agents(h._cfg())
    assert len(ag) == 1
    assert ag[0]["user_id"] == 999


def test_format_mentions_mixed():
    agents = [
        {"user_id": 1, "username": "", "display_name": "X"},
        {"user_id": 0, "username": "y", "display_name": "Y"},
    ]
    s = _format_mentions_line(agents, " ")
    assert "tg://user?id=1" in s
    assert "@y" in s
    assert "https://t.me/y" in s


def test_select_single_round_robin():
    class S:
        def round_robin_next_index(self, n, chat_id=None):
            return 1 % n

    agents = [
        {"user_id": 1, "display_name": "a"},
        {"user_id": 2, "display_name": "b"},
        {"user_id": 3, "display_name": "c"},
    ]
    cfg = {"mention_mode": "single_round_robin"}
    out = _select_agents_for_mention(cfg, agents, S())
    assert len(out) == 1
    assert out[0]["user_id"] == 2


def test_select_all_default():
    class S:
        def round_robin_next_index(self, n, chat_id=None):
            return 0

    agents = [{"user_id": 1}, {"user_id": 2}]
    out = _select_agents_for_mention({}, agents, S())
    assert len(out) == 2


def test_first_match_skips_team_with_no_agents(monkeypatch):
    monkeypatch.setattr(
        "src.utils.human_escalation.is_within_work_hours",
        lambda *a, **k: True,
    )
    cfg = {
        "team_pick_mode": "first_match",
        "agent_teams": [
            {"id": "empty", "agents": [], "work_hours": {}},
            {"id": "ok", "agents": [{"user_id": 3}], "work_hours": {}},
        ],
    }
    ag = _resolve_agents(cfg)
    assert len(ag) == 1
    assert ag[0]["user_id"] == 3


def test_team_pick_first_match(monkeypatch):
    monkeypatch.setattr(
        "src.utils.human_escalation.is_within_work_hours",
        lambda *a, **k: True,
    )
    cfg = {
        "timezone": "UTC",
        "team_pick_mode": "first_match",
        "agent_teams": [
            {"id": "a", "agents": [{"user_id": 1, "display_name": "A"}], "work_hours": {}},
            {"id": "b", "agents": [{"user_id": 2, "display_name": "B"}], "work_hours": {}},
        ],
    }
    ag = _resolve_agents(cfg)
    assert len(ag) == 1
    assert ag[0]["user_id"] == 1


def test_select_round_robin_per_chat_passes_chat_id():
    calls = []

    class S:
        def round_robin_next_index(self, n, chat_id=None):
            calls.append((n, chat_id))
            return 0

    cfg = {
        "mention_mode": "single_round_robin",
        "mention_round_robin_scope": "per_chat",
    }
    agents = [{"user_id": 1}, {"user_id": 2}]
    _select_agents_for_mention(cfg, agents, S(), "chat-99")
    assert calls[0] == (2, "chat-99")


def test_select_round_robin_global_when_chat_empty():
    calls = []

    class S:
        def round_robin_next_index(self, n, chat_id=None):
            calls.append((n, chat_id))
            return 0

    cfg = {
        "mention_mode": "single_round_robin",
        "mention_round_robin_scope": "per_chat",
    }
    _select_agents_for_mention(cfg, [{"user_id": 1}, {"user_id": 2}], S(), "")
    assert calls[0][1] is None


def test_agent_teams_when_schedule_matches(monkeypatch):
    monkeypatch.setattr(
        "src.utils.human_escalation.is_within_work_hours",
        lambda *a, **k: True,
    )
    cfg = {
        "timezone": "Asia/Shanghai",
        "work_hours": {},
        "agent_teams": [
            {
                "id": "day",
                "agents": [{"user_id": 7, "display_name": "T"}],
                "work_hours": {"mon": [["09:00", "18:00"]]},
            }
        ],
        "agents": [{"user_id": 1, "display_name": "G"}],
        "team_fallback_to_global": True,
    }
    ag = _resolve_agents(cfg)
    assert len(ag) == 1
    assert ag[0]["user_id"] == 7


def test_agent_teams_fallback_to_global(monkeypatch):
    monkeypatch.setattr(
        "src.utils.human_escalation.is_within_work_hours",
        lambda *a, **k: False,
    )
    cfg = {
        "timezone": "UTC",
        "agent_teams": [
            {
                "agents": [{"user_id": 99}],
                "work_hours": {"mon": [["09:00", "18:00"]]},
            }
        ],
        "agents": [{"user_id": 1, "display_name": "G"}],
        "team_fallback_to_global": True,
    }
    ag = _resolve_agents(cfg)
    assert ag[0]["user_id"] == 1


def test_active_teams_status_rows(monkeypatch):
    monkeypatch.setattr(
        "src.utils.human_escalation.is_within_work_hours",
        lambda *a, **k: True,
    )
    he = {
        "timezone": "UTC",
        "work_hours": {},
        "agent_teams": [
            {"id": "x", "name": "X队", "agents": [{"user_id": 1}], "work_hours": {}},
        ],
    }
    rows = active_teams_status(he)
    assert len(rows) == 1
    assert rows[0]["in_schedule"] is True
    assert rows[0]["agent_count"] == 1


def test_agent_teams_no_fallback_empty(monkeypatch):
    monkeypatch.setattr(
        "src.utils.human_escalation.is_within_work_hours",
        lambda *a, **k: False,
    )
    cfg = {
        "agent_teams": [
            {
                "agents": [{"user_id": 99}],
                "work_hours": {"mon": [["09:00", "18:00"]]},
            }
        ],
        "agents": [{"user_id": 1}],
        "team_fallback_to_global": False,
    }
    ag = _resolve_agents(cfg)
    assert ag == []


def test_suffix_when_always_duty(monkeypatch):
    store = _FakeStore()
    cfg = {
        "human_escalation": {
            "enabled": True,
            "repeat_threshold": 3,
            "cooldown_sec": 0,
            "duty_mode": "always",
            "agents": [{"user_id": 42, "display_name": "客服"}],
            "escalation_line": "请协助：",
        }
    }
    h = HumanEscalationHelper(cfg, store)
    out = h.format_suffix_if_needed(
        -100, 1, 3, "abc123", user_message_id=55, user_text="hi"
    )
    assert out.suffix
    assert "tg://user?id=42" in out.suffix
    assert out.forward_spec
    assert out.forward_spec["message_id"] == 55
    assert len(out.forward_spec["targets"]) == 1
    assert out.forward_spec["targets"][0]["user_id"] == 42


def test_suffix_includes_clickable_user_question():
    store = _FakeStore()
    cfg = {
        "human_escalation": {
            "enabled": True,
            "repeat_threshold": 3,
            "cooldown_sec": 0,
            "duty_mode": "always",
            "agents": [{"user_id": 42, "display_name": "客服"}],
            "escalation_line": "请协助：",
            "include_user_question_link": True,
        }
    }
    h = HumanEscalationHelper(cfg, store)
    out = h.format_suffix_if_needed(
        -1001234567890,
        1,
        3,
        "abc123",
        user_message_id=77,
        user_text="  我的订单怎么还没发货  ",
        chat_username=None,
    )
    assert out.suffix
    assert "tg://user?id=42" in out.suffix
    assert "https://t.me/c/1234567890/77" in out.suffix
    assert "我的订单怎么还没发货" in out.suffix
    assert '<a href="https://t.me/c/1234567890/77">' in out.suffix
    assert out.forward_spec and out.forward_spec["message_id"] == 77


def test_forward_spec_disabled_by_config():
    store = _FakeStore()
    cfg = {
        "human_escalation": {
            "enabled": True,
            "repeat_threshold": 3,
            "cooldown_sec": 0,
            "duty_mode": "always",
            "agents": [{"user_id": 42, "display_name": "客服"}],
            "escalation_line": "请协助：",
            "forward_user_message_to_agents": False,
        }
    }
    h = HumanEscalationHelper(cfg, store)
    out = h.format_suffix_if_needed(-100, 1, 3, "abc123", user_message_id=88)
    assert out.suffix
    assert out.forward_spec is None


def test_off_shift_suffix_has_no_forward_spec():
    store = _FakeStore()
    cfg = {
        "human_escalation": {
            "enabled": True,
            "repeat_threshold": 3,
            "cooldown_sec": 0,
            "duty_mode": "schedule",
            "timezone": "UTC",
            "work_hours": {},
            "agents": [{"user_id": 42, "display_name": "x"}],
            "message_off_shift": "当前不受理",
        }
    }
    h = HumanEscalationHelper(cfg, store)
    out = h.format_suffix_if_needed(-100, 1, 3, "nk", user_message_id=12)
    assert out.suffix == "\n\n当前不受理"
    assert out.forward_spec is None


def test_build_telegram_message_link_public_username():
    assert build_telegram_message_link(-1001, 5, "mygroup") == "https://t.me/mygroup/5"


def test_build_telegram_message_link_private_dm():
    u = build_telegram_message_link(12345, 9, None)
    assert u == "tg://openmessage?chat_id=12345&message_id=9"


def test_build_telegram_message_link_no_message_id():
    assert build_telegram_message_link(-100, 0, None) is None
    assert build_telegram_message_link(-100, None, None) is None


def test_normalize_user_question_strips_trailing_punctuation():
    assert normalize_user_question("订单有没有收到？") == normalize_user_question(
        "订单有没有收到"
    )
    assert normalize_user_question("  Hello??  ") == "hello"


def test_escalation_cooldown_by_norm_independent(tmp_path):
    """表 escalation_cooldown_by_norm：A 问句进入冷却后，B 问句仍可立即通过冷却检查。"""
    from src.utils.human_escalation_store import HumanEscalationStore
    import src.utils.human_escalation as he

    store = HumanEscalationStore(tmp_path / "he_cooldown.sqlite")
    nk_a = he._norm_key(he.normalize_user_question("句子甲重复问"))
    nk_b = he._norm_key(he.normalize_user_question("句子乙另一句"))
    ck, uk = "-1001", "501"
    assert store.cooldown_remaining_norm(ck, uk, nk_a, 60) == (True, 0.0)
    store.mark_escalation_norm(ck, uk, nk_a)
    assert store.cooldown_remaining_norm(ck, uk, nk_a, 60)[0] is False
    assert store.cooldown_remaining_norm(ck, uk, nk_b, 60) == (True, 0.0)
