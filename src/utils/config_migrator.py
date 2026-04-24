"""配置版本迁移工具 — 自动补充新版本字段，保留用户自定义值"""

import logging
import shutil
import time
import yaml
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("ConfigMigrator")

CURRENT_VERSION = "2.4"

_FIELD_DEFAULTS: Dict[str, Any] = {
    "rate_limit": {"enabled": False, "global": {"capacity": 30, "rate_per_sec": 2},
                   "per_chat": {"capacity": 20, "rate_per_sec": 1},
                   "per_user": {"capacity": 10, "rate_per_sec": 0.5}},
    "channel_alerts": {"enabled": False, "success_rate_threshold": 80},
    "scheduled_tasks": {"enabled": False, "tasks": []},
    "multi_bot": {"enabled": False, "default_session": "", "routes": []},
    "plugins": {"enabled": False, "disabled": []},
    "context_store": {"ttl_days": 30},
    "web_admin": {"enabled": False, "host": "127.0.0.1", "port": 8080,
                  "auth_token": "", "secret_key": "change-me-secret"},
    "webhook": {"enabled": False, "timeout": 10, "retry": 1, "webhooks": []},
    "ai_quality": {"enabled": True, "min_reply_length": 5,
                   "max_reply_length": 2000, "repeat_window": 10},
}

_DEPRECATED_FIELDS: List[str] = []


class ConfigMigrator:

    def __init__(self, config_path: Path):
        self._path = config_path

    def check_and_migrate(self) -> Tuple[bool, str]:
        if not self._path.exists():
            return False, "配置文件不存在"
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            return False, f"读取配置失败: {e}"

        current_ver = data.get("_config_version", "1.0")
        if current_ver == CURRENT_VERSION:
            return False, "配置已是最新版本"

        added = []
        for key, default in _FIELD_DEFAULTS.items():
            if key not in data:
                data[key] = default
                added.append(key)
            elif isinstance(default, dict) and isinstance(data[key], dict):
                for subkey, subval in default.items():
                    if subkey not in data[key]:
                        data[key][subkey] = subval
                        added.append(f"{key}.{subkey}")

        removed = []
        for dep_key in _DEPRECATED_FIELDS:
            if dep_key in data:
                removed.append(dep_key)

        roles_section = data.get("telegram", {}).get("quota_config_commands", {})
        if "roles" not in roles_section and roles_section.get("enabled"):
            roles_section["roles"] = {"super_admins": [], "admins": [], "operators": []}
            added.append("telegram.quota_config_commands.roles")

        data["_config_version"] = CURRENT_VERSION

        if not added and not removed:
            data["_config_version"] = CURRENT_VERSION
            self._save(data)
            return True, f"版本标记更新为 {CURRENT_VERSION}（无字段变更）"

        bak = self._backup()
        self._save(data)
        parts = []
        if added:
            parts.append(f"新增 {len(added)} 个字段: {', '.join(added[:10])}")
        if removed:
            parts.append(f"已废弃 {len(removed)} 个字段标记: {', '.join(removed)}")
        msg = f"配置已从 v{current_ver} 迁移到 v{CURRENT_VERSION}。{'; '.join(parts)}。备份: {bak.name}"
        logger.info(msg)
        return True, msg

    def _backup(self) -> Path:
        ts = time.strftime("%Y%m%d_%H%M%S")
        bak = self._path.with_suffix(f".yaml.bak_{ts}")
        shutil.copy2(self._path, bak)
        return bak

    def _save(self, data: dict):
        tmp = self._path.with_suffix(".yaml.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        tmp.replace(self._path)

    @staticmethod
    def get_version(config_path: Path) -> str:
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return data.get("_config_version", "1.0")
        except Exception:
            return "unknown"
