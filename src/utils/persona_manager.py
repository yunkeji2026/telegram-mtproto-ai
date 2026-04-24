"""
Persona Manager — handles loading, binding, and prompt assembly for personas.

Supports:
- Loading persona from domain pack persona.yaml
- Per-chat persona binding (different groups use different personas)
- Dynamic system prompt assembly: persona context + domain prompt + KB context
- Runtime persona override via Web admin API
"""

import logging
import copy
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger("PersonaManager")

RUNTIME_PERSONA_FILENAME = "persona_runtime.yaml"

# Default persona when none is configured
_DEFAULT_PERSONA: Dict[str, Any] = {
    "name": "Assistant",
    "role": "AI 助手",
    "personality": {
        "traits": ["友好", "专业"],
        "style": "自然聊天风格",
        "emoji_level": "moderate",
    },
    "speaking": {
        "openers": [],
        "forbidden_phrases": ["作为一个AI"],
        "reply_length": "moderate",
        "max_reply_sentences": 5,
        "language_follow": True,
    },
    "identity": {
        "deny_ai": False,
        "deny_ai_reply": "",
        "claim_human": False,
    },
    "boundaries": {
        "topics_to_avoid": [],
        "escalation_phrases": [],
    },
}


class PersonaManager:
    """Manages persona lifecycle, multi-group binding, and prompt assembly."""

    _instance: Optional["PersonaManager"] = None

    def __init__(self):
        self._default_persona: Dict[str, Any] = copy.deepcopy(_DEFAULT_PERSONA)
        self._chat_personas: Dict[str, Dict[str, Any]] = {}
        self._domain_persona: Optional[Dict[str, Any]] = None

    @classmethod
    def get_instance(cls) -> "PersonaManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls):
        cls._instance = None

    def set_domain_persona(self, persona_data: Dict[str, Any]):
        """Set the domain-level default persona (loaded from domain pack or runtime file)."""
        if persona_data:
            self._domain_persona = copy.deepcopy(persona_data)
            logger.info(
                "Domain persona set: name='%s', role='%s'",
                persona_data.get("name", "?"),
                persona_data.get("role", "?"),
            )

    @staticmethod
    def runtime_file_path(config_path: Path, explicit: str = "") -> Path:
        """persona_runtime 文件路径（与 config.yaml 同目录，除非显式指定相对/绝对路径）。"""
        base = Path(config_path).resolve().parent
        ex = (explicit or "").strip()
        if ex:
            p = Path(ex)
            return p if p.is_absolute() else (base / p)
        return base / RUNTIME_PERSONA_FILENAME

    def load_runtime_default_persona(
        self,
        config_path: Path,
        root_config: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        若存在 persona_runtime.yaml 且启用持久化配置，则加载并覆盖当前域默认人设。
        返回是否已应用覆盖。
        """
        root_config = root_config or {}
        pp = root_config.get("persona_persistence") or {}
        if not pp.get("enabled", True):
            return False
        path = self.runtime_file_path(
            config_path, str(pp.get("path") or "")
        )
        raw = self.load_persona_file(path)
        if not raw or not isinstance(raw, dict):
            return False
        pdata = raw.get("default_persona")
        if not isinstance(pdata, dict) or not pdata:
            if "name" in raw or "role" in raw:
                pdata = raw
            else:
                return False
        self.set_domain_persona(pdata)
        logger.info("已从 %s 加载运行时人设覆盖", path.name)
        return True

    def persist_default_persona(
        self,
        persona_data: Dict[str, Any],
        config_manager: Any,
    ) -> bool:
        """Web 保存默认人设后写入 persona_runtime.yaml（与 config 同目录）。"""
        if not persona_data or not config_manager:
            return False
        cfg_path = getattr(config_manager, "config_path", None)
        if not cfg_path:
            return False
        root = getattr(config_manager, "config", None) or {}
        pp = root.get("persona_persistence") or {}
        if not pp.get("enabled", True):
            return False
        path = self.runtime_file_path(Path(cfg_path), str(pp.get("path") or ""))
        wrapper = {
            "default_persona": copy.deepcopy(persona_data),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        ok = self.save_persona_file(path, wrapper)
        if ok:
            logger.info("人设已持久化到 %s", path)
        return ok

    def bind_chat_persona(self, chat_id: str, persona_data: Dict[str, Any]):
        """Bind a specific persona to a chat (group/private)."""
        self._chat_personas[str(chat_id)] = copy.deepcopy(persona_data)
        logger.info(
            "Chat %s bound to persona '%s'",
            chat_id, persona_data.get("name", "?"),
        )

    def unbind_chat_persona(self, chat_id: str):
        """Remove per-chat persona binding, falling back to domain default."""
        self._chat_personas.pop(str(chat_id), None)

    def get_persona(self, chat_id: str = "") -> Dict[str, Any]:
        """Get the effective persona for a chat, with fallback chain:
        chat-specific → domain default → global default
        """
        if chat_id and str(chat_id) in self._chat_personas:
            return self._chat_personas[str(chat_id)]
        if self._domain_persona:
            return self._domain_persona
        return self._default_persona

    def get_persona_name(self, chat_id: str = "") -> str:
        return self.get_persona(chat_id).get("name", "Assistant")

    def format_persona_block(
        self,
        chat_id: str = "",
        *,
        detail: str = "full",
        name_override: str = "",
    ) -> str:
        """供 AI 系统提示拼接。detail=full 完整；compact 仅核心句+禁忌，减轻与域 system_prompt 重复。
        name_override: 若 config 中配置了 ai.ai_name，应传入以覆盖域 persona.yaml 里的默认名，避免与主系统提示冲突。
        """
        p = self.get_persona(chat_id)
        if detail == "compact":
            return self._format_persona_compact(p, name_override=name_override)
        if detail == "none":
            return ""
        return self._format_persona_instructions(p, name_override=name_override)

    def _format_persona_compact(
        self, persona: Dict[str, Any], *, name_override: str = ""
    ) -> str:
        name = (name_override or "").strip() or persona.get("name", "Assistant")
        role = persona.get("role", "")
        lines: List[str] = [f"你是{name}，{role}。"]
        s = persona.get("speaking", {})
        forbidden = s.get("forbidden_phrases") or []
        if forbidden:
            lines.append(
                "禁止使用：" + "、".join(f"「{f}」" for f in forbidden[:12])
            )
        if s.get("language_follow"):
            lines.append("用户用什么语言发消息，就用同一语言回复。")
        return "\n".join(lines)

    def get_all_chat_bindings(self) -> Dict[str, str]:
        """Return {chat_id: persona_name} for all bound chats."""
        return {
            cid: p.get("name", "?")
            for cid, p in self._chat_personas.items()
        }

    # ── System prompt assembly ──────────────────────────────

    def build_system_prompt(
        self,
        chat_id: str = "",
        domain_prompt: str = "",
        kb_context: str = "",
        extra_context: str = "",
    ) -> str:
        """Assemble the full system prompt from persona + domain + KB.

        Assembly order:
        1. Persona identity & style instructions
        2. Domain-specific system prompt
        3. KB context (if any)
        4. Extra context (channel status, etc.)
        """
        persona = self.get_persona(chat_id)
        parts: List[str] = []

        # 1. Persona instructions
        persona_block = self._format_persona_instructions(persona)
        if persona_block:
            parts.append(persona_block)

        # 2. Domain prompt
        if domain_prompt:
            parts.append(domain_prompt)

        # 3. KB context
        if kb_context:
            parts.append(f"【知识库参考】\n{kb_context}")

        # 4. Extra context
        if extra_context:
            parts.append(extra_context)

        return "\n\n".join(parts)

    def _format_persona_instructions(
        self, persona: Dict[str, Any], *, name_override: str = ""
    ) -> str:
        """Convert persona.yaml into natural language instructions for the LLM."""
        lines: List[str] = []
        name = (name_override or "").strip() or persona.get("name", "Assistant")
        role = persona.get("role", "AI 助手")

        lines.append(f"你是{name}，{role}。")

        # Personality
        p = persona.get("personality", {})
        traits = p.get("traits", [])
        if traits:
            lines.append(f"性格特点：{'、'.join(traits)}。")
        style = p.get("style", "")
        if style:
            lines.append(f"说话风格：{style}。")

        # Speaking rules
        s = persona.get("speaking", {})
        forbidden = s.get("forbidden_phrases", [])
        if forbidden:
            lines.append(f"禁止使用以下表述：{'、'.join(f'「{f}」' for f in forbidden)}。")
        openers = s.get("openers", [])
        if openers:
            sample = "、".join(f"「{o}」" for o in openers[:6])
            lines.append(f"开头多样化，可选池：{sample}等。")
        forbidden_openers = s.get("forbidden_opener_words", [])
        if forbidden_openers:
            lines.append(f"避免以以下词开头：{'、'.join(f'「{f}」' for f in forbidden_openers)}。")
        max_sentences = s.get("max_reply_sentences", 0)
        if max_sentences:
            lines.append(f"单次回复建议不超过 {max_sentences} 句。")
        if s.get("language_follow"):
            lines.append("用户用什么语言发消息，就用同一语言回复。")

        # Identity
        identity = persona.get("identity", {})
        if identity.get("deny_ai"):
            reply = identity.get("deny_ai_reply", f"我是{name}")
            lines.append(f"当用户问你是不是AI时，回答：「{reply}」。")
        if identity.get("claim_human"):
            lines.append(f"你就是真人{role}{name}，说话要像真人在打字聊天。")

        # Boundaries
        b = persona.get("boundaries", {})
        avoid = b.get("topics_to_avoid", [])
        if avoid:
            lines.append(f"避免讨论以下话题：{'、'.join(avoid)}。")

        # Emotion handling
        e = persona.get("emotion", {})
        if e.get("frustrated_response"):
            lines.append(f"用户着急时：{e['frustrated_response']}。")

        return "\n".join(lines)

    # ── Persistence helpers ─────────────────────────────────

    def load_persona_file(self, path: Path) -> Optional[Dict[str, Any]]:
        """Load a persona from a YAML file."""
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return data
        except Exception as e:
            logger.warning("Failed to load persona from %s: %s", path, e)
            return None

    def save_persona_file(self, path: Path, persona: Dict[str, Any]) -> bool:
        """Save a persona to a YAML file."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                yaml.dump(
                    persona, f,
                    allow_unicode=True, default_flow_style=False, sort_keys=False,
                )
            return True
        except Exception as e:
            logger.warning("Failed to save persona to %s: %s", path, e)
            return False

    def export_chat_bindings(self) -> Dict[str, Any]:
        """Export all chat bindings for persistence."""
        return {
            "bindings": {
                cid: copy.deepcopy(p)
                for cid, p in self._chat_personas.items()
            }
        }

    def import_chat_bindings(self, data: Dict[str, Any]):
        """Import chat bindings from persisted data."""
        bindings = data.get("bindings", {})
        for cid, p in bindings.items():
            self._chat_personas[str(cid)] = copy.deepcopy(p)
        if bindings:
            logger.info("Imported %d chat persona bindings", len(bindings))
