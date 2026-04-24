"""额度配置技能 — 通过对话命令修改额度配置（特殊群/黑名单）"""

import re
from typing import Dict, Any, Optional

from src.skills.base import Skill


class QuotaConfigSkill(Skill):
    """通过对话命令修改额度配置（特殊群/黑名单）；仅对配置中允许的 user_id 生效"""

    def __init__(self, config, ai_client):
        super().__init__(config, ai_client)
        self.priority = 0

    async def execute(self, text: str, user_id: str, context: Dict[str, Any]) -> Optional[str]:
        tg = self.config.get_telegram_config() if hasattr(self.config, "get_telegram_config") else {}
        cmd_cfg = tg.get("quota_config_commands") or {}
        if not cmd_cfg.get("enabled"):
            return None
        allowed = cmd_cfg.get("allowed_user_ids") or []
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            uid = user_id
        if uid not in allowed and str(user_id) not in [str(a) for a in allowed]:
            return "无权限执行配置命令。"
        t = (text or "").strip()
        if "列出特殊群" in t:
            quota = getattr(self.config, "get_quota_rules", lambda: {})()
            groups = quota.get("special_groups") or []
            return "当前特殊客户群（%s 个）：\n" % len(groups) + "\n".join(groups) if groups else "当前无特殊客户群。"
        if "列出黑名单" in t:
            quota = getattr(self.config, "get_quota_rules", lambda: {})()
            bl = quota.get("blacklist_groups") or {}
            return "当前黑名单群（%s 个）：\n" % len(bl) + "\n".join(bl.keys()) if bl else "当前无黑名单群。"
        if "添加特殊群" in t:
            name = t.split("添加特殊群", 1)[-1].strip()
            if not name:
                return "请写：添加特殊群 <群名>（群名需与 Telegram 群标题完全一致）"
            ok, msg = getattr(self.config, "update_quota_rules_special_groups", lambda **kw: (False, "未支持"))(add=[name])
            return msg
        if "删除特殊群" in t:
            name = t.split("删除特殊群", 1)[-1].strip()
            if not name:
                return "请写：删除特殊群 <群名>"
            ok, msg = getattr(self.config, "update_quota_rules_special_groups", lambda **kw: (False, "未支持"))(remove=[name])
            return msg
        if "添加黑名单" in t:
            rest = t.split("添加黑名单", 1)[-1].strip()
            parts = rest.split(None, 1)
            name = (parts[0] or "").strip() if parts else ""
            if not name:
                return "请写：添加黑名单 <群名>（可选后跟 EP/JC 话术）"
            ep_text = jc_text = None
            if len(parts) > 1:
                ep_text = parts[1].strip()
            ok, msg = getattr(self.config, "update_quota_rules_blacklist", lambda **kw: (False, "未支持"))(add_group=name, ep_text=ep_text, jc_text=jc_text)
            return msg
        if "删除黑名单" in t:
            name = t.split("删除黑名单", 1)[-1].strip()
            if not name:
                return "请写：删除黑名单 <群名>"
            ok, msg = getattr(self.config, "update_quota_rules_blacklist", lambda **kw: (False, "未支持"))(remove_group=name)
            return msg
        return "支持的命令：列出特殊群、列出黑名单、添加特殊群 <群名>、删除特殊群 <群名>、添加黑名单 <群名>、删除黑名单 <群名>"
