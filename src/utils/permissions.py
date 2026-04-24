"""权限分级管理 — super_admin / admin / operator 三级角色"""

import logging
from typing import Dict, Optional, Set

logger = logging.getLogger("Permissions")

ROLE_SUPER_ADMIN = "super_admin"
ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"

ROLE_HIERARCHY = {ROLE_SUPER_ADMIN: 3, ROLE_ADMIN: 2, ROLE_OPERATOR: 1}

PERMISSION_MATRIX = {
    "view_config": {ROLE_OPERATOR, ROLE_ADMIN, ROLE_SUPER_ADMIN},
    "view_audit": {ROLE_OPERATOR, ROLE_ADMIN, ROLE_SUPER_ADMIN},
    "view_channel": {ROLE_OPERATOR, ROLE_ADMIN, ROLE_SUPER_ADMIN},
    "view_template": {ROLE_OPERATOR, ROLE_ADMIN, ROLE_SUPER_ADMIN},
    "list_snapshots": {ROLE_OPERATOR, ROLE_ADMIN, ROLE_SUPER_ADMIN},

    "edit_template": {ROLE_ADMIN, ROLE_SUPER_ADMIN},
    "edit_channel": {ROLE_ADMIN, ROLE_SUPER_ADMIN},
    "edit_special_group": {ROLE_ADMIN, ROLE_SUPER_ADMIN},
    "edit_blacklist": {ROLE_ADMIN, ROLE_SUPER_ADMIN},

    "batch_modify": {ROLE_SUPER_ADMIN},
    "rollback": {ROLE_SUPER_ADMIN},
    "undo": {ROLE_SUPER_ADMIN},
    "import_export": {ROLE_SUPER_ADMIN},
    "manage_users": {ROLE_SUPER_ADMIN},
}


class PermissionManager:

    def __init__(self, config: dict):
        self._roles: Dict[str, str] = {}
        self._legacy_allowed: list = []
        self._enabled = False
        self._load(config)

    def _load(self, config: dict):
        tg = config.get("telegram", {})
        cmd_cfg = tg.get("quota_config_commands", {})
        if not cmd_cfg.get("enabled"):
            return
        self._enabled = True
        self._legacy_allowed = [str(uid) for uid in (cmd_cfg.get("allowed_user_ids") or [])]

        rbac = cmd_cfg.get("roles", {})
        for uid in (rbac.get("super_admins") or []):
            self._roles[str(uid)] = ROLE_SUPER_ADMIN
        for uid in (rbac.get("admins") or []):
            self._roles.setdefault(str(uid), ROLE_ADMIN)
        for uid in (rbac.get("operators") or []):
            self._roles.setdefault(str(uid), ROLE_OPERATOR)

        for uid_str in self._legacy_allowed:
            if uid_str not in self._roles:
                self._roles[uid_str] = ROLE_SUPER_ADMIN

        if self._roles:
            logger.info("RBAC 已加载: %d 用户 (%s)",
                        len(self._roles),
                        ", ".join(f"{r}={sum(1 for v in self._roles.values() if v == r)}"
                                  for r in [ROLE_SUPER_ADMIN, ROLE_ADMIN, ROLE_OPERATOR]))

    @property
    def enabled(self) -> bool:
        return self._enabled

    def get_role(self, user_id) -> Optional[str]:
        uid = str(user_id)
        return self._roles.get(uid)

    def has_permission(self, user_id, permission: str) -> bool:
        role = self.get_role(user_id)
        if role is None:
            return False
        allowed_roles = PERMISSION_MATRIX.get(permission)
        if allowed_roles is None:
            return role == ROLE_SUPER_ADMIN
        return role in allowed_roles

    def check(self, user_id, permission: str) -> Optional[str]:
        if not self._enabled:
            return "config_disabled"
        role = self.get_role(user_id)
        if role is None:
            return "no_permission"
        if not self.has_permission(user_id, permission):
            return "insufficient_role"
        return None

    def get_role_display(self, user_id) -> str:
        role = self.get_role(user_id)
        names = {
            ROLE_SUPER_ADMIN: "超级管理员",
            ROLE_ADMIN: "管理员",
            ROLE_OPERATOR: "操作员",
        }
        return names.get(role, "无权限")
