"""插件系统 — 从 plugins/ 目录动态加载自定义 Skill"""

import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional, Type

logger = logging.getLogger("PluginLoader")


class PluginLoader:

    def __init__(self, plugin_dir: Path, config: dict = None):
        self._dir = plugin_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._config = config or {}
        self._loaded: Dict[str, object] = {}

    def discover(self) -> List[str]:
        found = []
        for f in self._dir.glob("*.py"):
            if f.name.startswith("_"):
                continue
            found.append(f.stem)
        return sorted(found)

    def load_all(self, skill_base_class, ai_client, config_manager) -> Dict[str, object]:
        plugins_cfg = self._config.get("plugins", {})
        if not plugins_cfg.get("enabled", False):
            return {}
        disabled = set(plugins_cfg.get("disabled", []))
        results = {}
        for name in self.discover():
            if name in disabled:
                logger.debug("插件 %s 已禁用，跳过", name)
                continue
            try:
                skill = self._load_one(name, skill_base_class, ai_client, config_manager)
                if skill:
                    results[name] = skill
                    self._loaded[name] = skill
                    logger.info("插件加载成功: %s", name)
            except Exception as e:
                logger.warning("插件加载失败 %s: %s", name, e)
        return results

    def _load_one(self, name: str, skill_base_class, ai_client, config_manager) -> Optional[object]:
        module_path = self._dir / f"{name}.py"
        if not module_path.exists():
            return None
        spec = importlib.util.spec_from_file_location(f"plugins.{name}", str(module_path))
        if not spec or not spec.loader:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"plugins.{name}"] = module
        spec.loader.exec_module(module)

        skill_class = getattr(module, "PluginSkill", None)
        if skill_class is None:
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (isinstance(attr, type) and issubclass(attr, skill_base_class)
                        and attr is not skill_base_class):
                    skill_class = attr
                    break
        if skill_class is None:
            logger.warning("插件 %s 中未找到 Skill 子类", name)
            return None
        return skill_class(config_manager, ai_client)

    def reload_plugin(self, name: str, skill_base_class, ai_client, config_manager) -> Optional[object]:
        mod_key = f"plugins.{name}"
        if mod_key in sys.modules:
            del sys.modules[mod_key]
        skill = self._load_one(name, skill_base_class, ai_client, config_manager)
        if skill:
            self._loaded[name] = skill
            logger.info("插件热重载成功: %s", name)
        return skill

    def list_plugins(self) -> List[Dict]:
        discovered = self.discover()
        disabled = set(self._config.get("plugins", {}).get("disabled", []))
        result = []
        for name in discovered:
            result.append({
                "name": name,
                "loaded": name in self._loaded,
                "disabled": name in disabled,
            })
        return result
