"""
Domain Pack Loader — reads manifest.yaml from domain directories and registers
skills, config overrides, KB categories, prompt templates, trigger keywords,
and i18n keys into the running system.
"""

import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

import yaml

logger = logging.getLogger("DomainLoader")


class DomainPack:
    """Represents a loaded domain pack with all its components."""

    def __init__(self, name: str, root: Path, manifest: dict):
        self.name = name
        self.root = root
        self.manifest = manifest
        self.display_name: str = manifest.get("display_name", name)
        self.version: str = manifest.get("version", "0.0")
        self.description: str = manifest.get("description", "")

        self.skill_classes: Dict[str, Type] = {}
        self.config_data: Dict[str, Any] = {}
        self.kb_categories: List[dict] = []
        self.kb_seeds: List[dict] = []
        self.system_prompt: str = ""
        self.terminology: Dict[str, str] = {}
        self.context_supplements: Dict[str, Any] = {}
        self.trigger_keywords: Dict[str, Any] = {}
        self.i18n: Dict[str, Dict[str, str]] = {}
        self.hook_class: Optional[Type] = None
        self.persona: Dict[str, Any] = {}
        self.web_pages: List[dict] = []
        self.web_dashboard_widgets: List[dict] = []
        self.web_routes_enabled: bool = False

    def __repr__(self):
        return f"<DomainPack '{self.name}' v{self.version}>"


class DomainLoader:
    """
    Discovers and loads domain packs from the domains/ directory.
    Each domain is a subdirectory with a manifest.yaml.
    """

    def __init__(self, domains_dir: Path):
        self._domains_dir = domains_dir
        self._loaded: Dict[str, DomainPack] = {}

    def discover(self) -> List[str]:
        """Return names of all domain directories that contain a manifest.yaml."""
        if not self._domains_dir.exists():
            return []
        found = []
        for d in sorted(self._domains_dir.iterdir()):
            if d.is_dir() and (d / "manifest.yaml").exists():
                found.append(d.name)
        return found

    def load(self, domain_name: str, skill_base_class: Type, ai_client, config_manager) -> Optional[DomainPack]:
        """Load a single domain pack by name."""
        domain_dir = self._domains_dir / domain_name
        manifest_path = domain_dir / "manifest.yaml"

        if not manifest_path.exists():
            logger.error("Domain '%s' has no manifest.yaml", domain_name)
            return None

        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = yaml.safe_load(f) or {}
        except Exception as e:
            logger.error("Failed to parse manifest for domain '%s': %s", domain_name, e)
            return None

        pack = DomainPack(domain_name, domain_dir, manifest)

        self._load_skills(pack, skill_base_class, ai_client, config_manager)
        self._load_config(pack)
        self._load_kb(pack)
        self._load_prompts(pack)
        self._load_trigger_keywords(pack)
        self._load_i18n(pack)
        self._load_hooks(pack, config_manager)
        self._load_persona(pack)
        self._load_web(pack)

        self._loaded[domain_name] = pack
        logger.info(
            "Domain '%s' loaded: %d skills, %d KB categories, hook=%s, persona=%s",
            domain_name, len(pack.skill_classes), len(pack.kb_categories),
            pack.hook_class.__name__ if pack.hook_class else "none",
            bool(pack.persona),
        )
        return pack

    def get(self, domain_name: str) -> Optional[DomainPack]:
        return self._loaded.get(domain_name)

    @property
    def loaded_domains(self) -> Dict[str, DomainPack]:
        return dict(self._loaded)

    # ── Internal loaders ──────────────────────────────────────

    def _load_skills(self, pack: DomainPack, skill_base_class, ai_client, config_manager):
        """Import skill classes from the domain's skills/ package."""
        skills_dir = pack.root / "skills"
        if not skills_dir.exists():
            return

        skill_names = pack.manifest.get("skills", [])
        if not skill_names:
            return

        pkg_name = f"domains.{pack.name}.skills"
        try:
            skills_module = importlib.import_module(pkg_name)
        except ImportError as e:
            logger.warning("Cannot import skills package for domain '%s': %s", pack.name, e)
            return

        for skill_name in skill_names:
            class_name = self._to_class_name(skill_name)
            cls = getattr(skills_module, class_name, None)
            if cls is None:
                # Try loading from individual module
                try:
                    mod = importlib.import_module(f"{pkg_name}.{skill_name}")
                    for attr in dir(mod):
                        obj = getattr(mod, attr)
                        if (isinstance(obj, type) and issubclass(obj, skill_base_class)
                                and obj is not skill_base_class):
                            cls = obj
                            break
                except ImportError:
                    pass

            if cls is not None:
                pack.skill_classes[skill_name] = cls
                logger.debug("Domain '%s': registered skill class %s", pack.name, class_name)
            else:
                logger.warning("Domain '%s': skill class '%s' not found", pack.name, class_name)

    def _load_config(self, pack: DomainPack):
        """Load domain config files into pack.config_data."""
        config_dir = pack.root / "config"
        if not config_dir.exists():
            return

        config_files = pack.manifest.get("config_files", [])
        for fname in config_files:
            fpath = config_dir / fname
            if fpath.exists() and fpath.suffix in (".yaml", ".yml"):
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = yaml.safe_load(f) or {}
                    pack.config_data[fpath.stem] = data
                except Exception as e:
                    logger.warning("Domain '%s': failed to load config '%s': %s", pack.name, fname, e)

    def _load_kb(self, pack: DomainPack):
        """Load KB categories and seeds from the domain's kb/ directory."""
        kb_section = pack.manifest.get("kb", {})
        if not kb_section:
            return

        categories_path = pack.root / kb_section.get("categories", "kb/categories.yaml")
        if categories_path.exists():
            try:
                with open(categories_path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                pack.kb_categories = data.get("categories", [])
            except Exception as e:
                logger.warning("Domain '%s': failed to load KB categories: %s", pack.name, e)

        seeds_path = pack.root / kb_section.get("seeds", "kb/seeds.yaml")
        if seeds_path.exists():
            try:
                with open(seeds_path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                pack.kb_seeds = data.get("system_reply_seeds", [])
            except Exception as e:
                logger.warning("Domain '%s': failed to load KB seeds: %s", pack.name, e)

    def _load_prompts(self, pack: DomainPack):
        """Load system prompt, terminology, and context supplements."""
        prompts_section = pack.manifest.get("prompts", {})
        if not prompts_section:
            return

        prompt_path = pack.root / prompts_section.get("system_prompt", "prompts/system_prompt.txt")
        if prompt_path.exists():
            try:
                pack.system_prompt = prompt_path.read_text(encoding="utf-8")
            except Exception as e:
                logger.warning("Domain '%s': failed to load system prompt: %s", pack.name, e)

        term_path = pack.root / prompts_section.get("terminology", "prompts/terminology.yaml")
        if term_path.exists():
            try:
                with open(term_path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                pack.terminology = data.get("corrections", {})
            except Exception as e:
                logger.warning("Domain '%s': failed to load terminology: %s", pack.name, e)

        supp_path = pack.root / prompts_section.get("context_supplements", "prompts/context_supplements.yaml")
        if supp_path.exists():
            try:
                with open(supp_path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                pack.context_supplements = data.get("intents", {})
            except Exception as e:
                logger.warning("Domain '%s': failed to load context supplements: %s", pack.name, e)

    def _load_trigger_keywords(self, pack: DomainPack):
        """Load domain-specific trigger keywords."""
        config_dir = pack.root / "config"
        kw_path = config_dir / "trigger_keywords.yaml"
        if kw_path.exists():
            try:
                with open(kw_path, "r", encoding="utf-8") as f:
                    pack.trigger_keywords = yaml.safe_load(f) or {}
            except Exception as e:
                logger.warning("Domain '%s': failed to load trigger keywords: %s", pack.name, e)

    def _load_i18n(self, pack: DomainPack):
        """Load domain-specific i18n translation keys."""
        i18n_section = pack.manifest.get("i18n", {})
        if not i18n_section:
            return

        for lang, fpath_str in i18n_section.items():
            fpath = pack.root / fpath_str
            if fpath.exists():
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = yaml.safe_load(f) or {}
                    # Filter out comment-only keys
                    pack.i18n[lang] = {k: v for k, v in data.items() if not k.startswith("#")}
                except Exception as e:
                    logger.warning("Domain '%s': failed to load i18n '%s': %s", pack.name, lang, e)

    def _load_hooks(self, pack: DomainPack, config_manager):
        """Load domain hook class from hooks.py."""
        hooks_path = pack.root / "hooks.py"
        if not hooks_path.exists():
            return

        module_name = f"domains.{pack.name}.hooks"
        try:
            mod = importlib.import_module(module_name)
        except ImportError:
            try:
                spec = importlib.util.spec_from_file_location(module_name, hooks_path)
                if spec and spec.loader:
                    mod = importlib.util.module_from_spec(spec)
                    sys.modules[module_name] = mod
                    spec.loader.exec_module(mod)
                else:
                    return
            except Exception as e:
                logger.warning("Domain '%s': failed to load hooks: %s", pack.name, e)
                return

        from src.hooks.base import DomainHook
        for attr_name in dir(mod):
            obj = getattr(mod, attr_name)
            if (isinstance(obj, type)
                    and issubclass(obj, DomainHook)
                    and obj is not DomainHook):
                pack.hook_class = obj
                logger.debug("Domain '%s': found hook class %s", pack.name, attr_name)
                break

    def _load_persona(self, pack: DomainPack):
        """Load persona.yaml from domain pack if it exists."""
        persona_path = pack.root / "persona.yaml"
        if not persona_path.exists():
            return
        try:
            with open(persona_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            pack.persona = data
            logger.debug("Domain '%s': loaded persona '%s'", pack.name, data.get("name", ""))
        except Exception as e:
            logger.warning("Domain '%s': failed to load persona: %s", pack.name, e)

    def _load_web(self, pack: DomainPack):
        """Load web plugin declarations (pages, widgets, routes flag)."""
        web_section = pack.manifest.get("web", {})
        if not web_section:
            legacy = pack.manifest.get("web_routes", False)
            pack.web_routes_enabled = bool(legacy)
            return
        pack.web_routes_enabled = bool(web_section.get("routes", False))
        pack.web_pages = web_section.get("pages", [])
        pack.web_dashboard_widgets = web_section.get("dashboard_widgets", [])

    @staticmethod
    def _to_class_name(skill_name: str) -> str:
        """Convert snake_case skill name to PascalCase class name with 'Skill' suffix.
        e.g. 'gxp_command' -> 'GxpCommandSkill'
        """
        parts = skill_name.split("_")
        pascal = "".join(p.capitalize() for p in parts)
        if not pascal.endswith("Skill"):
            pascal += "Skill"
        return pascal
