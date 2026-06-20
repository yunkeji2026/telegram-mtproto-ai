"""人设一致性守卫单测：禁用语/AI 自曝身份的检测与按句剥离（陪聊沉浸感保护）。"""

from src.utils.persona_guard import collect_forbidden, find_violations, sanitize


def _persona(phrases=None, deny_ai=False):
    return {
        "speaking": {"forbidden_phrases": list(phrases or [])},
        "identity": {"deny_ai": deny_ai},
    }


# ── collect_forbidden ────────────────────────────────────────────────────────

def test_collect_forbidden_extracts():
    fb = collect_forbidden(_persona(["有什么可以帮您的", " "], deny_ai=True))
    assert fb["phrases"] == ["有什么可以帮您的"]  # 空白项被过滤
    assert fb["deny_ai"] is True


def test_collect_forbidden_empty_persona():
    fb = collect_forbidden({})
    assert fb["phrases"] == []
    assert fb["deny_ai"] is False


# ── find_violations ──────────────────────────────────────────────────────────

def test_find_phrase_violation():
    p = _persona(["有什么可以帮您的"])
    hits = find_violations("亲，有什么可以帮您的吗？", p)
    assert "有什么可以帮您的" in hits


def test_find_phrase_whitespace_insensitive():
    p = _persona(["有什么可以帮您的"])
    hits = find_violations("有什么  可以帮您的", p)  # 中间夹空格仍命中
    assert hits


def test_find_ai_self_id_when_deny_ai():
    p = _persona(deny_ai=True)
    assert find_violations("其实我是一个人工智能。", p)
    assert find_violations("作为AI，我建议你……", p)
    assert find_violations("As an AI, I cannot do that.", p)


def test_ai_self_id_ignored_when_deny_ai_false():
    p = _persona(deny_ai=False)
    assert find_violations("我是一个人工智能", p) == []


def test_negation_not_flagged():
    p = _persona(deny_ai=True)
    # "我不是 AI" 是否定句，不算露馅
    assert find_violations("哈哈我不是AI啦", p) == []


def test_clean_reply_no_violation():
    p = _persona(["有什么可以帮您的"], deny_ai=True)
    assert find_violations("好呀～今天过得怎么样？", p) == []


# ── sanitize ─────────────────────────────────────────────────────────────────

def test_sanitize_removes_offending_sentence_keeps_rest():
    p = _persona(deny_ai=True)
    txt = "好呀～想你了。作为一个人工智能我来帮你。要不要聊聊？"
    cleaned, violations = sanitize(txt, p)
    assert violations
    assert "人工智能" not in cleaned
    assert "想你了" in cleaned
    assert "要不要聊聊" in cleaned


def test_sanitize_removes_helpdesk_phrase_sentence():
    p = _persona(["有什么可以帮您的"])
    txt = "亲，有什么可以帮您的吗？我在呢。"
    cleaned, violations = sanitize(txt, p)
    assert "有什么可以帮您的" not in cleaned
    assert "我在呢" in cleaned


def test_sanitize_no_config_returns_original():
    cleaned, violations = sanitize("作为一个人工智能我来帮你", _persona())
    assert violations == []
    assert cleaned == "作为一个人工智能我来帮你"  # 未声明 deny_ai/禁用语 → 不动


def test_sanitize_all_violating_falls_back_never_empty():
    p = _persona(deny_ai=True)
    txt = "作为一个人工智能。"
    cleaned, violations = sanitize(txt, p)
    assert violations
    assert cleaned  # 绝不返回空串
    assert cleaned.strip() != ""


def test_sanitize_clean_text_untouched():
    p = _persona(["有什么可以帮您的"], deny_ai=True)
    txt = "好呀～想你了，今天累不累？"
    cleaned, violations = sanitize(txt, p)
    assert violations == []
    assert cleaned == txt


def test_sanitize_empty_text():
    cleaned, violations = sanitize("", _persona(["x"], deny_ai=True))
    assert cleaned == ""
    assert violations == []


# ── SkillManager 接线（_enforce_persona_consistency）─────────────────────────

class _FakePM:
    def __init__(self, persona):
        self._persona = persona

    def get_persona(self, chat_id="", account_persona_id=""):
        return self._persona


def _bare_sm():
    from src.skills.skill_manager import SkillManager
    return SkillManager.__new__(SkillManager)


def test_skillmanager_guard_strips(monkeypatch):
    from src.utils.persona_manager import PersonaManager
    persona = {"speaking": {"forbidden_phrases": ["有什么可以帮您的"]},
               "identity": {"deny_ai": True}}
    monkeypatch.setattr(PersonaManager, "get_instance", lambda: _FakePM(persona))
    sm = _bare_sm()
    sm._persona_guard_enabled = True
    out = sm._enforce_persona_consistency(
        "亲，有什么可以帮您的吗？我在呢。", chat_id="1")
    assert "有什么可以帮您的" not in out
    assert "我在呢" in out


def test_skillmanager_guard_disabled_passthrough(monkeypatch):
    from src.utils.persona_manager import PersonaManager
    persona = {"speaking": {"forbidden_phrases": ["有什么可以帮您的"]},
               "identity": {"deny_ai": True}}
    monkeypatch.setattr(PersonaManager, "get_instance", lambda: _FakePM(persona))
    sm = _bare_sm()
    sm._persona_guard_enabled = False
    txt = "亲，有什么可以帮您的吗？"
    assert sm._enforce_persona_consistency(txt, chat_id="1") == txt


def test_skillmanager_guard_swallows_errors(monkeypatch):
    from src.utils.persona_manager import PersonaManager

    def _boom():
        raise RuntimeError("pm down")

    monkeypatch.setattr(PersonaManager, "get_instance", _boom)
    sm = _bare_sm()
    sm._persona_guard_enabled = True
    txt = "好呀～想你了"
    # 守卫异常必须回退原文，绝不冒泡
    assert sm._enforce_persona_consistency(txt, chat_id="1") == txt
