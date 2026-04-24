"""
Tests for the Persona system — Phase 0B.

Tests:
1. PersonaManager singleton lifecycle
2. Default persona fallback
3. Domain persona loading
4. Per-chat persona binding
5. System prompt assembly
6. Persona file I/O
7. DomainLoader persona loading integration
8. Multi-group multi-persona scenario
"""

import pytest
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.persona_manager import PersonaManager


class TestPersonaManagerLifecycle:
    def setup_method(self):
        PersonaManager.reset()

    def test_singleton(self):
        p1 = PersonaManager.get_instance()
        p2 = PersonaManager.get_instance()
        assert p1 is p2

    def test_reset(self):
        p1 = PersonaManager.get_instance()
        PersonaManager.reset()
        p2 = PersonaManager.get_instance()
        assert p1 is not p2


class TestDefaultPersona:
    def setup_method(self):
        PersonaManager.reset()

    def test_default_name(self):
        pm = PersonaManager.get_instance()
        assert pm.get_persona_name() == "Assistant"

    def test_default_persona_structure(self):
        pm = PersonaManager.get_instance()
        persona = pm.get_persona()
        assert "name" in persona
        assert "role" in persona
        assert "personality" in persona
        assert "speaking" in persona
        assert "identity" in persona

    def test_default_persona_is_not_none(self):
        pm = PersonaManager.get_instance()
        assert pm.get_persona() is not None


class TestDomainPersona:
    def setup_method(self):
        PersonaManager.reset()

    def test_set_domain_persona(self):
        pm = PersonaManager.get_instance()
        pm.set_domain_persona({
            "name": "Camille",
            "role": "支付客服",
            "personality": {"traits": ["友好"]},
        })
        assert pm.get_persona_name() == "Camille"
        assert pm.get_persona()["role"] == "支付客服"

    def test_domain_persona_overrides_default(self):
        pm = PersonaManager.get_instance()
        pm.set_domain_persona({"name": "TestBot", "role": "测试"})
        assert pm.get_persona_name() != "Assistant"
        assert pm.get_persona_name() == "TestBot"

    def test_domain_persona_deep_copy(self):
        """Modifying returned persona shouldn't affect stored copy."""
        pm = PersonaManager.get_instance()
        original = {"name": "Bot", "role": "test", "personality": {"traits": ["kind"]}}
        pm.set_domain_persona(original)
        original["name"] = "MODIFIED"
        assert pm.get_persona_name() == "Bot"


class TestChatPersonaBinding:
    def setup_method(self):
        PersonaManager.reset()

    def test_bind_chat_persona(self):
        pm = PersonaManager.get_instance()
        pm.set_domain_persona({"name": "Default", "role": "default"})
        pm.bind_chat_persona("12345", {"name": "GroupBot", "role": "群管"})
        assert pm.get_persona_name("12345") == "GroupBot"
        assert pm.get_persona_name("99999") == "Default"
        assert pm.get_persona_name() == "Default"

    def test_unbind_chat_persona(self):
        pm = PersonaManager.get_instance()
        pm.set_domain_persona({"name": "Default", "role": "default"})
        pm.bind_chat_persona("12345", {"name": "Custom", "role": "custom"})
        assert pm.get_persona_name("12345") == "Custom"
        pm.unbind_chat_persona("12345")
        assert pm.get_persona_name("12345") == "Default"

    def test_multi_group_different_personas(self):
        """Core test: different groups use different personas simultaneously."""
        pm = PersonaManager.get_instance()
        pm.set_domain_persona({"name": "BaseBot", "role": "base"})
        pm.bind_chat_persona("group_a", {"name": "SalesBot", "role": "销售"})
        pm.bind_chat_persona("group_b", {"name": "SupportBot", "role": "技术支持"})
        pm.bind_chat_persona("group_c", {"name": "CryptoBot", "role": "行情分析"})

        assert pm.get_persona_name("group_a") == "SalesBot"
        assert pm.get_persona_name("group_b") == "SupportBot"
        assert pm.get_persona_name("group_c") == "CryptoBot"
        assert pm.get_persona_name("group_d") == "BaseBot"

    def test_get_all_chat_bindings(self):
        pm = PersonaManager.get_instance()
        pm.bind_chat_persona("111", {"name": "A"})
        pm.bind_chat_persona("222", {"name": "B"})
        bindings = pm.get_all_chat_bindings()
        assert bindings == {"111": "A", "222": "B"}


class TestSystemPromptAssembly:
    def setup_method(self):
        PersonaManager.reset()

    def test_minimal_prompt(self):
        pm = PersonaManager.get_instance()
        pm.set_domain_persona({"name": "Bot", "role": "助手"})
        prompt = pm.build_system_prompt()
        assert "Bot" in prompt
        assert "助手" in prompt

    def test_prompt_with_domain(self):
        pm = PersonaManager.get_instance()
        pm.set_domain_persona({"name": "Bot", "role": "助手"})
        prompt = pm.build_system_prompt(domain_prompt="你负责订单查询。")
        assert "Bot" in prompt
        assert "订单查询" in prompt

    def test_prompt_with_kb_context(self):
        pm = PersonaManager.get_instance()
        pm.set_domain_persona({"name": "Bot", "role": "助手"})
        prompt = pm.build_system_prompt(kb_context="EP通道正常，成功率95%")
        assert "知识库参考" in prompt
        assert "EP通道正常" in prompt

    def test_prompt_with_all_parts(self):
        pm = PersonaManager.get_instance()
        pm.set_domain_persona({
            "name": "Camille",
            "role": "客服",
            "personality": {"traits": ["友好", "专业"], "style": "自然聊天"},
            "speaking": {
                "openers": ["在的", "好的呀"],
                "forbidden_phrases": ["作为一个AI"],
                "max_reply_sentences": 4,
                "language_follow": True,
            },
            "identity": {"deny_ai": True, "deny_ai_reply": "我是客服Camille"},
        })
        prompt = pm.build_system_prompt(
            domain_prompt="负责支付客服。",
            kb_context="EP通道正常",
            extra_context="当前有3个活跃通道",
        )
        assert "Camille" in prompt
        assert "友好" in prompt
        assert "专业" in prompt
        assert "自然聊天" in prompt
        assert "作为一个AI" in prompt
        assert "在的" in prompt
        assert "4" in prompt
        assert "我是客服Camille" in prompt
        assert "支付客服" in prompt
        assert "EP通道正常" in prompt
        assert "3个活跃通道" in prompt

    def test_chat_specific_prompt(self):
        """Different chats get different persona instructions."""
        pm = PersonaManager.get_instance()
        pm.set_domain_persona({"name": "Default", "role": "助手"})
        pm.bind_chat_persona("group_1", {"name": "TechSupport", "role": "技术支持"})

        prompt_default = pm.build_system_prompt(chat_id="group_2")
        prompt_custom = pm.build_system_prompt(chat_id="group_1")

        assert "Default" in prompt_default
        assert "TechSupport" in prompt_custom
        assert "技术支持" in prompt_custom


class TestPersonaFileIO:
    def test_save_and_load(self, tmp_path):
        pm = PersonaManager.get_instance()
        persona = {
            "name": "FileBot",
            "role": "测试",
            "personality": {"traits": ["test"]},
        }
        path = tmp_path / "test_persona.yaml"
        assert pm.save_persona_file(path, persona)
        loaded = pm.load_persona_file(path)
        assert loaded is not None
        assert loaded["name"] == "FileBot"

    def test_load_nonexistent(self):
        pm = PersonaManager.get_instance()
        result = pm.load_persona_file(Path("/nonexistent/path.yaml"))
        assert result is None

    def test_export_import_bindings(self):
        PersonaManager.reset()
        pm = PersonaManager.get_instance()
        pm.bind_chat_persona("111", {"name": "A", "role": "a"})
        pm.bind_chat_persona("222", {"name": "B", "role": "b"})

        exported = pm.export_chat_bindings()

        PersonaManager.reset()
        pm2 = PersonaManager.get_instance()
        pm2.import_chat_bindings(exported)

        assert pm2.get_persona_name("111") == "A"
        assert pm2.get_persona_name("222") == "B"


class TestDomainLoaderPersona:
    def test_load_payment_persona(self):
        from src.utils.domain_loader import DomainLoader, DomainPack
        project_root = Path(__file__).parent.parent
        domains_dir = project_root / "domains"
        loader = DomainLoader(domains_dir)

        import yaml
        manifest_path = domains_dir / "payment" / "manifest.yaml"
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = yaml.safe_load(f)

        pack = DomainPack("payment", domains_dir / "payment", manifest)
        loader._load_persona(pack)

        assert pack.persona is not None
        assert pack.persona.get("name") == "小优"
        assert pack.persona.get("role") == "支付通道客服专员"
        assert "speaking" in pack.persona
        assert "identity" in pack.persona


class TestFormatPersonaInstructions:
    def setup_method(self):
        PersonaManager.reset()

    def test_full_persona_formatting(self):
        pm = PersonaManager.get_instance()
        persona = {
            "name": "TestBot",
            "role": "测试助手",
            "personality": {
                "traits": ["认真", "负责"],
                "style": "简洁直接",
            },
            "speaking": {
                "openers": ["好的", "收到"],
                "forbidden_phrases": ["作为AI"],
                "forbidden_opener_words": ["嗯"],
                "max_reply_sentences": 3,
                "language_follow": True,
            },
            "identity": {
                "deny_ai": True,
                "deny_ai_reply": "我是TestBot",
                "claim_human": True,
            },
            "boundaries": {
                "topics_to_avoid": ["政治"],
            },
            "emotion": {
                "frustrated_response": "先安抚再解决",
            },
        }
        result = pm._format_persona_instructions(persona)
        assert "TestBot" in result
        assert "测试助手" in result
        assert "认真" in result
        assert "简洁直接" in result
        assert "作为AI" in result
        assert "好的" in result
        assert "嗯" in result
        assert "3" in result
        assert "同一语言" in result
        assert "我是TestBot" in result
        assert "真人" in result
        assert "政治" in result
        assert "先安抚" in result
