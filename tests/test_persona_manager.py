"""PersonaManager 单测（无外部依赖）。"""

from src.utils.persona_manager import PersonaManager


def test_format_persona_block_uses_domain():
    PersonaManager.reset()
    pm = PersonaManager.get_instance()
    pm.set_domain_persona(
        {"name": "测试名", "role": "测试角色", "personality": {"traits": ["a"]}}
    )
    b = pm.format_persona_block("")
    assert "测试名" in b
    assert "测试角色" in b


def test_format_persona_block_empty_ok():
    PersonaManager.reset()
    pm = PersonaManager.get_instance()
    b = pm.format_persona_block("")
    assert "Assistant" in b or len(b) > 0


def test_format_persona_block_compact():
    PersonaManager.reset()
    pm = PersonaManager.get_instance()
    pm.set_domain_persona(
        {
            "name": "N",
            "role": "R",
            "speaking": {"forbidden_phrases": ["a", "b"], "language_follow": True},
        }
    )
    c = pm.format_persona_block("", detail="compact")
    assert "N" in c and "R" in c and "禁止" in c
    assert pm.format_persona_block("", detail="none") == ""


def test_runtime_roundtrip(tmp_path):
    PersonaManager.reset()
    pm = PersonaManager.get_instance()
    cfg = tmp_path / "config.yaml"
    cfg.write_text("persona_persistence:\n  enabled: true\n", encoding="utf-8")
    pm.set_domain_persona({"name": "X", "role": "Y"})
    class CM:
        config_path = cfg
        config = {"persona_persistence": {"enabled": True}}

    assert pm.persist_default_persona({"name": "X2", "role": "Y2"}, CM())
    assert PersonaManager.runtime_file_path(cfg).exists()
    PersonaManager.reset()
    pm2 = PersonaManager.get_instance()
    pm2.set_domain_persona({"name": "orig", "role": "orig"})
    assert pm2.load_runtime_default_persona(cfg, {"persona_persistence": {"enabled": True}})
    assert pm2.get_persona("").get("name") == "X2"
