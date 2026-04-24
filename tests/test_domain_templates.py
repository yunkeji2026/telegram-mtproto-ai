"""
Tests for Domain Pack templates — Phase 1A.

Validates that all domain packs (payment, ecommerce, community, education,
crypto, it_helpdesk, legal, general, conversion) have valid structure and can be loaded.
"""

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml
from src.utils.domain_loader import DomainLoader, DomainPack
from src.hooks.base import DomainHook

DOMAINS_DIR = Path(__file__).parent.parent / "domains"
ALL_DOMAINS = ["payment", "ecommerce", "community", "education", "crypto",
               "it_helpdesk", "legal", "general", "conversion"]


class TestDomainDiscovery:
    def test_discover_all_domains(self):
        loader = DomainLoader(DOMAINS_DIR)
        found = loader.discover()
        for d in ALL_DOMAINS:
            assert d in found, f"Domain '{d}' not discovered"

    def test_each_domain_has_manifest(self):
        for d in ALL_DOMAINS:
            manifest_path = DOMAINS_DIR / d / "manifest.yaml"
            assert manifest_path.exists(), f"{d}/manifest.yaml missing"


class TestManifestStructure:
    @pytest.fixture(params=ALL_DOMAINS)
    def domain_manifest(self, request):
        d = request.param
        with open(DOMAINS_DIR / d / "manifest.yaml", "r", encoding="utf-8") as f:
            manifest = yaml.safe_load(f)
        return d, manifest

    def test_has_required_fields(self, domain_manifest):
        name, manifest = domain_manifest
        assert "name" in manifest, f"{name}: missing 'name'"
        assert "display_name" in manifest, f"{name}: missing 'display_name'"
        assert "version" in manifest, f"{name}: missing 'version'"
        assert "description" in manifest, f"{name}: missing 'description'"

    def test_name_matches_directory(self, domain_manifest):
        name, manifest = domain_manifest
        assert manifest["name"] == name

    def test_has_prompts_section(self, domain_manifest):
        name, manifest = domain_manifest
        assert "prompts" in manifest, f"{name}: missing 'prompts'"
        prompts = manifest["prompts"]
        assert "system_prompt" in prompts, f"{name}: missing system_prompt path"


class TestPersonaFiles:
    @pytest.fixture(params=ALL_DOMAINS)
    def domain_name(self, request):
        return request.param

    def test_persona_exists_for_new_domains(self, domain_name):
        if domain_name == "ecommerce":
            pytest.skip("ecommerce is legacy skeleton, persona optional")
        persona_path = DOMAINS_DIR / domain_name / "persona.yaml"
        assert persona_path.exists(), f"{domain_name}/persona.yaml missing"

    def test_persona_has_name_and_role(self, domain_name):
        persona_path = DOMAINS_DIR / domain_name / "persona.yaml"
        if not persona_path.exists():
            pytest.skip(f"{domain_name} has no persona.yaml")
        with open(persona_path, "r", encoding="utf-8") as f:
            persona = yaml.safe_load(f)
        assert "name" in persona, f"{domain_name}: persona missing 'name'"
        assert "role" in persona, f"{domain_name}: persona missing 'role'"


class TestPromptFiles:
    @pytest.fixture(params=ALL_DOMAINS)
    def domain_name(self, request):
        return request.param

    def test_system_prompt_exists(self, domain_name):
        prompt_path = DOMAINS_DIR / domain_name / "prompts" / "system_prompt.txt"
        assert prompt_path.exists(), f"{domain_name}/prompts/system_prompt.txt missing"

    def test_system_prompt_not_empty(self, domain_name):
        prompt_path = DOMAINS_DIR / domain_name / "prompts" / "system_prompt.txt"
        if not prompt_path.exists():
            pytest.skip("no system_prompt.txt")
        content = prompt_path.read_text(encoding="utf-8")
        assert len(content) > 50, f"{domain_name}: system_prompt.txt too short ({len(content)} chars)"


class TestKBFiles:
    @pytest.fixture(params=ALL_DOMAINS)
    def domain_name(self, request):
        return request.param

    def test_categories_yaml_exists(self, domain_name):
        if domain_name == "ecommerce":
            pytest.skip("ecommerce legacy")
        path = DOMAINS_DIR / domain_name / "kb" / "categories.yaml"
        assert path.exists(), f"{domain_name}/kb/categories.yaml missing"

    def test_categories_has_list(self, domain_name):
        path = DOMAINS_DIR / domain_name / "kb" / "categories.yaml"
        if not path.exists():
            pytest.skip("no categories.yaml")
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert "categories" in data, f"{domain_name}: categories.yaml missing 'categories' key"
        assert isinstance(data["categories"], list)
        assert len(data["categories"]) >= 3, f"{domain_name}: fewer than 3 categories"

    def test_seeds_yaml_exists(self, domain_name):
        if domain_name == "ecommerce":
            pytest.skip("ecommerce legacy")
        path = DOMAINS_DIR / domain_name / "kb" / "seeds.yaml"
        assert path.exists(), f"{domain_name}/kb/seeds.yaml missing"

    def test_seeds_has_entries(self, domain_name):
        path = DOMAINS_DIR / domain_name / "kb" / "seeds.yaml"
        if not path.exists():
            pytest.skip("no seeds.yaml")
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        seeds = data.get("system_reply_seeds", [])
        assert len(seeds) >= 3, f"{domain_name}: fewer than 3 KB seeds"


class TestHookFiles:
    @pytest.fixture(params=ALL_DOMAINS)
    def domain_name(self, request):
        return request.param

    def test_hooks_loadable(self, domain_name):
        if domain_name == "ecommerce":
            pytest.skip("ecommerce has no hooks")
        hooks_path = DOMAINS_DIR / domain_name / "hooks.py"
        if not hooks_path.exists():
            pytest.skip(f"{domain_name} has no hooks.py")

        loader = DomainLoader(DOMAINS_DIR)
        manifest_path = DOMAINS_DIR / domain_name / "manifest.yaml"
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = yaml.safe_load(f)

        pack = DomainPack(domain_name, DOMAINS_DIR / domain_name, manifest)
        loader._load_hooks(pack, None)

        assert pack.hook_class is not None, f"{domain_name}: no hook class found"
        assert issubclass(pack.hook_class, DomainHook), f"{domain_name}: hook not a DomainHook subclass"

    def test_hook_instantiable(self, domain_name):
        if domain_name == "ecommerce":
            pytest.skip("ecommerce has no hooks")
        hooks_path = DOMAINS_DIR / domain_name / "hooks.py"
        if not hooks_path.exists():
            pytest.skip(f"{domain_name} has no hooks.py")

        loader = DomainLoader(DOMAINS_DIR)
        manifest_path = DOMAINS_DIR / domain_name / "manifest.yaml"
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = yaml.safe_load(f)

        pack = DomainPack(domain_name, DOMAINS_DIR / domain_name, manifest)
        loader._load_hooks(pack, None)
        instance = pack.hook_class()
        assert instance is not None


class TestI18nFiles:
    @pytest.fixture(params=ALL_DOMAINS)
    def domain_name(self, request):
        return request.param

    def test_zh_i18n_exists(self, domain_name):
        if domain_name == "ecommerce":
            pytest.skip("ecommerce may have different structure")
        path = DOMAINS_DIR / domain_name / "i18n" / "zh.yaml"
        assert path.exists(), f"{domain_name}/i18n/zh.yaml missing"

    def test_en_i18n_exists(self, domain_name):
        if domain_name == "ecommerce":
            pytest.skip("ecommerce may have different structure")
        path = DOMAINS_DIR / domain_name / "i18n" / "en.yaml"
        assert path.exists(), f"{domain_name}/i18n/en.yaml missing"


class TestFullDomainLoad:
    """Integration test: load each domain pack through the full DomainLoader."""

    @pytest.fixture(params=[d for d in ALL_DOMAINS if d != "ecommerce"])
    def domain_name(self, request):
        return request.param

    def test_full_load(self, domain_name):
        loader = DomainLoader(DOMAINS_DIR)
        manifest_path = DOMAINS_DIR / domain_name / "manifest.yaml"
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = yaml.safe_load(f)

        pack = DomainPack(domain_name, DOMAINS_DIR / domain_name, manifest)
        loader._load_config(pack)
        loader._load_kb(pack)
        loader._load_prompts(pack)
        loader._load_i18n(pack)
        loader._load_hooks(pack, None)
        loader._load_persona(pack)

        assert pack.system_prompt, f"{domain_name}: system_prompt empty after load"
        assert pack.persona, f"{domain_name}: persona empty after load"
        if domain_name != "ecommerce":
            assert pack.hook_class is not None, f"{domain_name}: no hook class after load"
