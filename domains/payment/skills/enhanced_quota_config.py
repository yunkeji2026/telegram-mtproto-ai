"""增强配置管理技能 — 话术模板、通道汇率、配置查看、审计回滚"""

import re
import time
from pathlib import Path
from typing import Dict, Any, Optional

from src.utils.audit_store import AuditStore
from src.skills.base import Skill
from .quota_config import QuotaConfigSkill


class EnhancedQuotaConfigSkill(QuotaConfigSkill):
    """
    增强的配置管理技能：话术模板、通道汇率、配置查看、审计回滚。
    继承自 QuotaConfigSkill，复用其权限验证和特殊群/黑名单命令。
    """

    _CHANNEL_ALIASES: Dict[str, str] = {
        "ep": "ep", "easypaisa": "ep", "easy": "ep",
        "jc": "jc", "jazzcash": "jc", "jazz": "jc", "jp": "jc", "jz": "jc",
    }

    def __init__(self, config, ai_client):
        super().__init__(config, ai_client)
        self.priority = -1
        self._legacy_jsonl = self._resolve_audit_path()
        cfg_dir = self._legacy_jsonl.parent
        self._audit = AuditStore(
            db_path=cfg_dir / "audit.db",
            legacy_jsonl_path=self._legacy_jsonl,
        )
        self._snapshot_dir = cfg_dir / "snapshots"
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        from src.utils.permissions import PermissionManager
        self._perm = PermissionManager(config.config if hasattr(config, "config") else {})

    # ── 权限 & 入口 ────────────────────────────────────────────

    def _check_permission(self, user_id: str, permission: str = "view_config") -> Optional[str]:
        tg = self.config.get_telegram_config() if hasattr(self.config, "get_telegram_config") else {}
        cmd_cfg = tg.get("quota_config_commands") or {}
        if not cmd_cfg.get("enabled"):
            return "config_disabled"
        return self._perm.check(user_id, permission)

    async def execute(self, text: str, user_id: str, context: Dict[str, Any]) -> Optional[str]:
        base_perm = self._check_permission(user_id, "view_config")
        if base_perm == "config_disabled":
            return None
        if base_perm in ("no_permission", "insufficient_role"):
            return None

        t = (text or "").strip()

        if re.search(r"我的角色|我的权限|查看权限", t):
            return f"你的角色: {self._perm.get_role_display(user_id)}"

        user_ctx = context.get("user_context", {})
        if re.search(r"确认批量修改|确认执行", t):
            err = self._check_permission(user_id, "batch_modify")
            if err:
                return "权限不足: 仅超级管理员可执行批量修改。"
            batch = user_ctx.pop("batch_pending", None)
            if batch:
                return await self._execute_batch(str(user_id), batch)

        original = await super().execute(text, user_id, context)
        if original and original != "无权限执行配置命令。" and not original.startswith("支持的命令："):
            return original

        if self._is_batch_cmd(t):
            err = self._check_permission(user_id, "batch_modify")
            if err:
                return "权限不足: 仅超级管理员可执行批量修改。"
            return await self._handle_batch_modification(t, str(user_id), context)
        if self._is_template_cmd(t):
            if re.search(r"更新|修改|添加|删除|换|调整", t):
                err = self._check_permission(user_id, "edit_template")
                if err:
                    return "权限不足: 操作员无法修改话术模板。"
            return await self._handle_template_management(t, user_id)
        if self._is_channel_cmd(t):
            if re.search(r"更新|修改|启用|禁用|改|设为", t):
                err = self._check_permission(user_id, "edit_channel")
                if err:
                    return "权限不足: 操作员无法修改通道配置。"
            return await self._handle_exchange_rate_management(t, user_id)
        if self._is_view_cmd(t):
            return await self._handle_config_view(t)
        if re.search(r"查看操作记录|操作记录|操作日志", t):
            return self._show_audit_log(t)
        if re.search(r"撤销上次|撤销操作", t):
            err = self._check_permission(user_id, "undo")
            if err:
                return "权限不足: 仅超级管理员可执行撤销操作。"
            return self._undo_last(str(user_id))
        m_rb = re.search(r"回滚\s+(templates_\d+|exchange_rates_\d+)", t)
        if m_rb:
            err = self._check_permission(user_id, "rollback")
            if err:
                return "权限不足: 仅超级管理员可执行回滚。"
            return self._rollback_snapshot(m_rb.group(1), str(user_id))
        if re.search(r"列出快照|查看快照", t):
            snaps = sorted(self._snapshot_dir.glob("*.yaml"))[-15:]
            if not snaps:
                return "暂无快照。"
            lines = ["可用快照\n"] + [f"  - {s.stem}" for s in snaps]
            return "\n".join(lines)
        if re.search(r"^(配置帮助|管理帮助)$", t):
            return self._get_help_message()

        return None

    # ── 命令识别 ───────────────────────────────────────────────

    @staticmethod
    def _is_template_cmd(t: str) -> bool:
        return bool(re.search(
            r"话术|模板|问候语|回复模板|更新话术|修改模板|添加话术|删除话术|列出话术|改一下|换种说法|调整话术",
            t
        ))

    @staticmethod
    def _is_channel_cmd(t: str) -> bool:
        return bool(re.search(
            r"费率|启用通道|禁用通道|更新额度|列出通道|查看通道|通道配置|通道状态",
            t
        )) or bool(re.search(r"更新汇率|修改汇率|改汇率|汇率改为|查看汇率|列出汇率", t))

    @staticmethod
    def _is_view_cmd(t: str) -> bool:
        return bool(re.search(r"查看配置|当前配置|配置列表|显示配置|配置信息|看看配置|配置摘要|查看话术|查看汇率", t))

    # ── 话术模板管理 ───────────────────────────────────────────

    async def _handle_template_management(self, text: str, user_id: str = "") -> str:
        data = self.config.get_dynamic_templates_config()
        if not data:
            return "未找到话术模板配置文件。"

        if re.search(r"^列出话术", text):
            return self._list_templates(data)

        m = re.match(r"查看话术\s+(\S+)", text)
        if m:
            return self._view_template(data, m.group(1))

        m = re.match(r"更新话术\s+(\S+)\s+(.+)", text, re.DOTALL)
        if m:
            return self._update_template(data, m.group(1), m.group(2).strip(), user_id)

        m = re.match(r"添加话术\s+(\S+)\s+(.+)", text, re.DOTALL)
        if m:
            return self._add_template_item(data, m.group(1), m.group(2).strip(), user_id)

        m = re.match(r"删除话术\s+(\S+)\s+(\d+)", text)
        if m:
            return self._delete_template_item(data, m.group(1), int(m.group(2)), user_id)

        nl = re.search(r"(?:把|将)\s*(\S+?)\s*(?:改成|改为|换成)\s*(.+)", text, re.DOTALL)
        if nl:
            tpl_name = nl.group(1)
            tpl_content = nl.group(2).strip()
            if tpl_name in data and tpl_content:
                return self._update_template(data, tpl_name, tpl_content, user_id)

        return ("话术管理命令格式：\n"
                "- 列出话术\n"
                "- 查看话术 <模板名>\n"
                "- 更新话术 <模板名> <新内容>\n"
                "- 添加话术 <模板名> <新内容>\n"
                "- 删除话术 <模板名> <序号>")

    def _list_templates(self, data: dict) -> str:
        lines = ["话术模板列表\n"]
        for key, val in data.items():
            if isinstance(val, list):
                lines.append(f"  - {key} ({len(val)} 条)")
            elif isinstance(val, str):
                lines.append(f"  - {key} (单条)")
            elif isinstance(val, dict):
                lines.append(f"  - {key} (字典)")
        return "\n".join(lines) if len(lines) > 1 else "暂无话术模板。"

    def _view_template(self, data: dict, name: str) -> str:
        val = data.get(name)
        if val is None:
            return f"模板 '{name}' 不存在。可用模板：{', '.join(data.keys())}"
        if isinstance(val, list):
            items = [f"  {i+1}. {v}" for i, v in enumerate(val)]
            return f"{name}（共 {len(val)} 条）\n" + "\n".join(items)
        return f"{name}\n  {val}"

    def _update_template(self, data: dict, name: str, content: str, user_id: str) -> str:
        if name not in data:
            return f"模板 '{name}' 不存在。可用模板：{', '.join(data.keys())}"
        if len(content) < 2 or len(content) > 500:
            return "内容长度须在 2-500 字符之间。"
        snap = self._save_snapshot("templates")
        old = data[name]
        old_preview = old[0] if isinstance(old, list) and old else str(old)[:60]
        if isinstance(data[name], list):
            data[name][0] = content
        else:
            data[name] = content
        ok, msg = self.config.save_templates(data)
        if not ok:
            return f"保存失败：{msg}"
        self._log_audit(user_id, "update_template", name, old_preview, content[:60], snap)
        return f"已更新话术 {name} 第 1 条\n旧: {old_preview[:50]}...\n新: {content[:50]}..."

    def _add_template_item(self, data: dict, name: str, content: str, user_id: str) -> str:
        if name not in data:
            return f"模板 '{name}' 不存在。可用模板：{', '.join(data.keys())}"
        if len(content) < 2 or len(content) > 500:
            return "内容长度须在 2-500 字符之间。"
        snap = self._save_snapshot("templates")
        if isinstance(data[name], list):
            data[name].append(content)
            idx = len(data[name])
        else:
            data[name] = [data[name], content]
            idx = 2
        ok, msg = self.config.save_templates(data)
        if not ok:
            return f"保存失败：{msg}"
        self._log_audit(user_id, "add_template", name, "", content[:60], snap)
        return f"已添加话术 {name} 第 {idx} 条：{content[:50]}..."

    def _delete_template_item(self, data: dict, name: str, idx: int, user_id: str) -> str:
        if name not in data:
            return f"模板 '{name}' 不存在。"
        val = data[name]
        if not isinstance(val, list):
            return f"模板 '{name}' 不是列表类型，无法按序号删除。"
        if idx < 1 or idx > len(val):
            return f"序号须在 1-{len(val)} 之间。"
        snap = self._save_snapshot("templates")
        removed = val.pop(idx - 1)
        ok, msg = self.config.save_templates(data)
        if not ok:
            return f"保存失败：{msg}"
        self._log_audit(user_id, "delete_template", f"{name}#{idx}", removed[:60], "", snap)
        return f"已删除话术 {name} 第 {idx} 条：{removed[:50]}..."

    # ── 通道 / 汇率管理 ───────────────────────────────────────

    async def _handle_exchange_rate_management(self, text: str, user_id: str = "") -> str:
        data = self.config.get_exchange_rates_config()
        channels = data.get("channels") or {}
        if not channels:
            return "未找到通道配置。"

        if re.search(r"^(列出通道|列出汇率|通道状态)", text):
            return self._list_channels(channels)

        m = re.match(r"查看(通道|汇率)\s*(\S+)?", text)
        if m and m.group(2):
            return self._view_channel(channels, m.group(2))

        m = re.match(r"更新(汇率|费率)\s+(\S+)\s+(\S+)", text)
        if m:
            return self._update_channel_rate(data, channels, m.group(2), m.group(3), user_id)

        m = re.match(r"更新额度\s+(\S+)\s+(\S+)", text)
        if m:
            return self._update_channel_limits(data, channels, m.group(1), m.group(2), user_id)

        m = re.match(r"(启用|禁用)通道\s+(\S+)", text)
        if m:
            action = "正常" if m.group(1) == "启用" else "暂停"
            return self._update_channel_status(data, channels, m.group(2), action, user_id)

        nl = self._parse_natural_language_rate(text)
        if nl:
            ch_name, rate = nl
            return self._update_channel_rate(data, channels, ch_name, rate, user_id)

        return ("通道管理命令格式：\n"
                "- 列出通道\n"
                "- 查看通道 <通道名>\n"
                "- 更新费率 <通道名> <费率>\n"
                "- 更新额度 <通道名> <范围>\n"
                "- 启用通道 <通道名>\n"
                "- 禁用通道 <通道名>")

    def _parse_natural_language_rate(self, text: str) -> Optional[tuple]:
        m = re.search(
            r"(ep|jc|jazz|easy|easypaisa|jazzcash)\s*(?:的)?(?:费率|汇率)\s*(?:改为|改成|调到|调整为|设为|设成)\s*([\d.]+%?)",
            text, re.IGNORECASE
        )
        if m:
            return m.group(1), m.group(2)
        return None

    def _resolve_channel(self, channels: dict, name: str) -> Optional[str]:
        key = self._CHANNEL_ALIASES.get(name.lower())
        if key and key in channels:
            return key
        if name.lower() in channels:
            return name.lower()
        for k, cfg in channels.items():
            aliases = cfg.get("names") or []
            if name.upper() in [a.upper() for a in aliases]:
                return k
        return None

    @staticmethod
    def _dir_get(cfg: dict, direction: str, field: str):
        sub = cfg.get(direction)
        if isinstance(sub, dict) and field in sub:
            return sub[field]
        return cfg.get(field)

    def _list_channels(self, channels: dict) -> str:
        lines = ["通道状态\n"]
        for key, cfg in channels.items():
            display = cfg.get("display_name", key)
            pi_st = self._dir_get(cfg, "payin", "status") or "未知"
            po_st = self._dir_get(cfg, "payout", "status") or "未知"
            status = pi_st if pi_st == po_st else f"代收{pi_st}/代付{po_st}"
            pi_fee = self._dir_get(cfg, "payin", "fee_rate") or "-"
            po_fee = self._dir_get(cfg, "payout", "fee_rate") or "-"
            fee = pi_fee if pi_fee == po_fee else f"代收{pi_fee}/代付{po_fee}"
            lim = (cfg.get("limits") or {}).get("default", "-")
            lines.append(f"  - {display}: {status} | 费率 {fee} | 额度 {lim}")
        return "\n".join(lines)

    def _view_channel(self, channels: dict, name: str) -> str:
        key = self._resolve_channel(channels, name)
        if not key:
            avail = ", ".join(c.get("display_name", k) for k, c in channels.items())
            return f"通道 '{name}' 不存在。可用通道：{avail}"
        cfg = channels[key]
        parts = [cfg.get('display_name', key)]
        for d, label in [("payin", "代收"), ("payout", "代付")]:
            sub = cfg.get(d)
            if isinstance(sub, dict):
                parts.append(f"  [{label}]")
                parts.append(f"    状态: {sub.get('status', '-')}")
                parts.append(f"    费率: {sub.get('fee_rate', '-')}")
                parts.append(f"    成功率: {sub.get('success_rate', '-')}%")
                parts.append(f"    限额: {sub.get('minimum_amount', '-')}-{sub.get('maximum_amount', '-')}")
                pt = sub.get("processing_time") or cfg.get("processing_time", "")
                parts.append(f"    处理时间: {pt or '-'}")
                d_amt = sub.get("amount_type") or cfg.get("amount_type", "-")
                parts.append(f"    金额类型: {d_amt}")
        if not cfg.get("payin") and not cfg.get("payout"):
            parts.append(f"  状态: {cfg.get('status', '-')}")
            parts.append(f"  费率: {cfg.get('fee_rate', '-')}")
            parts.append(f"  处理时间: {cfg.get('processing_time', '-')}")
            parts.append(f"  金额类型: {cfg.get('amount_type', '-')}")
        parts.append(f"  额度: {(cfg.get('limits') or {}).get('default', '-')}")
        parts.append(f"  备注: {cfg.get('notes', '-')}")
        return "\n".join(parts)

    def _update_channel_rate(self, data: dict, channels: dict, name: str, rate: str, user_id: str) -> str:
        key = self._resolve_channel(channels, name)
        if not key:
            return f"通道 '{name}' 不存在。"
        snap = self._save_snapshot("exchange_rates")
        ch = channels[key]
        old_parts = []
        for d in ("payin", "payout"):
            sub = ch.get(d)
            if isinstance(sub, dict):
                old_parts.append(f"{d}={sub.get('fee_rate', '-')}")
                sub["fee_rate"] = rate
            else:
                old_parts.append(ch.get("fee_rate", "-"))
                ch["fee_rate"] = rate
        old = ", ".join(old_parts) if old_parts else "-"
        ch["last_updated"] = time.strftime("%Y-%m-%d")
        data["channels"] = channels
        ok, msg = self.config.save_exchange_rates(data)
        if not ok:
            return f"保存失败：{msg}"
        display = ch.get("display_name", key)
        self._log_audit(user_id, "update_rate", display, old, rate, snap)
        return f"已更新 {display} 代收代付费率\n旧: {old}\n新: {rate}"

    def _update_channel_limits(self, data: dict, channels: dict, name: str, limits: str, user_id: str) -> str:
        key = self._resolve_channel(channels, name)
        if not key:
            return f"通道 '{name}' 不存在。"
        if not re.match(r"\d[\d,]*\s*[-~]\s*\d[\d,]*", limits):
            return "额度格式须为 数字-数字，如 100-50000"
        snap = self._save_snapshot("exchange_rates")
        old = (channels[key].get("limits") or {}).get("default", "-")
        if "limits" not in channels[key]:
            channels[key]["limits"] = {}
        channels[key]["limits"]["default"] = limits
        channels[key]["last_updated"] = time.strftime("%Y-%m-%d")
        data["channels"] = channels
        ok, msg = self.config.save_exchange_rates(data)
        if not ok:
            return f"保存失败：{msg}"
        display = channels[key].get("display_name", key)
        self._log_audit(user_id, "update_limits", display, old, limits, snap)
        return f"已更新 {display} 额度\n旧: {old}\n新: {limits}"

    def _update_channel_status(self, data: dict, channels: dict, name: str, status: str, user_id: str) -> str:
        key = self._resolve_channel(channels, name)
        if not key:
            return f"通道 '{name}' 不存在。"
        snap = self._save_snapshot("exchange_rates")
        ch = channels[key]
        old_parts = []
        for d in ("payin", "payout"):
            sub = ch.get(d)
            if isinstance(sub, dict):
                old_parts.append(f"{d}={sub.get('status', '-')}")
                sub["status"] = status
        if not old_parts:
            old_parts.append(ch.get("status", "-"))
            ch["status"] = status
        old = ", ".join(old_parts)
        ch["last_updated"] = time.strftime("%Y-%m-%d")
        data["channels"] = channels
        ok, msg = self.config.save_exchange_rates(data)
        if not ok:
            return f"保存失败：{msg}"
        display = ch.get("display_name", key)
        self._log_audit(user_id, "update_status", display, old, status, snap)
        return f"已更新 {display} 代收代付状态为 {status}"

    # ── 配置查看 ───────────────────────────────────────────────

    async def _handle_config_view(self, text: str) -> str:
        if "话术" in text:
            data = self.config.get_dynamic_templates_config()
            return self._list_templates(data) if data else "话术模板文件未找到。"
        if "汇率" in text or "通道" in text:
            data = self.config.get_exchange_rates_config()
            channels = data.get("channels") or {}
            return self._list_channels(channels) if channels else "通道配置文件未找到。"

        tpl = self.config.get_dynamic_templates_config() or {}
        tpl_count = sum(len(v) if isinstance(v, list) else 1 for v in tpl.values())
        rate = self.config.get_exchange_rates_config() or {}
        ch = rate.get("channels") or {}
        def _ch_status_str(c):
            pi = (c.get("payin") or {}).get("status") if isinstance(c.get("payin"), dict) else c.get("status")
            po = (c.get("payout") or {}).get("status") if isinstance(c.get("payout"), dict) else c.get("status")
            return pi if pi == po else f"{pi}/{po}"
        ch_summary = ", ".join(f"{c.get('display_name', k)}: {_ch_status_str(c)}" for k, c in ch.items())
        quota = getattr(self.config, "get_quota_rules", lambda: {})()
        sg = len(quota.get("special_groups") or [])
        bl = len(quota.get("blacklist_groups") or [])
        tg = self.config.get_telegram_config() if hasattr(self.config, "get_telegram_config") else {}
        cd = (self.config.config.get("skills") or {}).get("cooldown") or {}
        gxp = "已启用" if (tg.get("gxp_commands") or {}).get("enabled") else "未启用"

        return (f"系统配置摘要\n\n"
                f"话术模板：{len(tpl)} 类，共 {tpl_count} 条\n"
                f"通道：{len(ch)} 个（{ch_summary}）\n"
                f"特殊群：{sg} 个\n"
                f"黑名单：{bl} 个\n"
                f"冷却：全局 {cd.get('global', '?')}s / 同内容 {cd.get('per_content', '?')}s\n"
                f"GXP 代发：{gxp}")

    # ── 审计持久化 ──────────────────────────────────────────────

    def _resolve_audit_path(self) -> Path:
        cfg_dir = Path(self.config.config_path).parent if hasattr(self.config, "config_path") else Path("config")
        return cfg_dir / "audit_log.jsonl"

    def _log_audit(self, user_id: str, action: str, target: str, old_val: str, new_val: str,
                   snapshot_id: str = ""):
        self._audit.log(user_id=user_id, action=action, target=target,
                        old_val=old_val, new_val=new_val, snapshot_id=snapshot_id)

    def _read_audit_entries(self, limit: int = 50) -> list:
        return self._audit.query(limit=limit)

    def _show_audit_log(self, text: str) -> str:
        m = re.search(r"(\d+)", text)
        limit = int(m.group(1)) if m else 10
        entries = self._read_audit_entries(limit)
        if not entries:
            return "暂无操作记录。"
        lines = [f"最近 {len(entries)} 条操作记录\n"]
        for e in reversed(entries):
            lines.append(f"  [{e.get('ts','')}] {e.get('action','')} → {e.get('target','')}")
            if e.get("old_val"):
                lines.append(f"    旧: {e['old_val'][:40]}")
            if e.get("new_val"):
                lines.append(f"    新: {e['new_val'][:40]}")
            if e.get("snapshot_id"):
                lines.append(f"    快照: {e['snapshot_id']}")
        return "\n".join(lines)

    # ── 版本快照 & 撤销 ──────────────────────────────────────

    def _save_snapshot(self, file_key: str) -> str:
        src = None
        if file_key == "templates":
            src = getattr(self.config, "_get_templates_file_path", lambda: None)()
        elif file_key == "exchange_rates":
            src = getattr(self.config, "_get_exchange_rates_file_path", lambda: None)()
        if not src or not src.exists():
            return ""
        snap_id = f"{file_key}_{int(time.time())}"
        dst = self._snapshot_dir / f"{snap_id}.yaml"
        try:
            import shutil
            shutil.copy2(src, dst)
            self._trim_snapshots(file_key, keep=10)
            return snap_id
        except Exception as e:
            self.logger.warning("保存快照失败: %s", e)
            return ""

    def _trim_snapshots(self, prefix: str, keep: int = 10):
        files = sorted(self._snapshot_dir.glob(f"{prefix}_*.yaml"))
        for old in files[:-keep]:
            old.unlink(missing_ok=True)

    def _rollback_snapshot(self, snap_id: str, user_id: str) -> str:
        snap_file = self._snapshot_dir / f"{snap_id}.yaml"
        if not snap_file.exists():
            available = [f.stem for f in sorted(self._snapshot_dir.glob("*.yaml"))[-10:]]
            hint = "\n".join(f"  - {s}" for s in available) if available else "  (无快照)"
            return f"快照 {snap_id} 不存在。可用快照：\n{hint}"
        if snap_id.startswith("templates_"):
            target = getattr(self.config, "_get_templates_file_path", lambda: None)()
            cache_fn = "invalidate_templates_cache"
        elif snap_id.startswith("exchange_rates_"):
            target = getattr(self.config, "_get_exchange_rates_file_path", lambda: None)()
            cache_fn = "invalidate_exchange_rates_cache"
        else:
            return f"无法识别快照类型: {snap_id}"
        if not target:
            return "目标配置文件不存在。"
        try:
            import shutil
            before_snap = self._save_snapshot(snap_id.split("_")[0])
            shutil.copy2(snap_file, target)
            getattr(self.config, cache_fn, lambda: None)()
            self._log_audit(user_id, "rollback", snap_id, before_snap, snap_id)
            return f"已回滚到快照 {snap_id}"
        except Exception as e:
            return f"回滚失败: {e}"

    def _undo_last(self, user_id: str) -> str:
        entries = self._read_audit_entries(20)
        for e in reversed(entries):
            snap = e.get("snapshot_id") or e.get("snap") or ""
            if snap and e.get("action") != "rollback":
                return self._rollback_snapshot(snap, user_id)
        return "没有找到可撤销的操作（需要有快照记录）。"

    # ── 批量 / 自然语言增强 ──────────────────────────────────────

    @staticmethod
    def _is_batch_cmd(t: str) -> bool:
        return bool(re.search(
            r"所有通道|全部通道|统一|批量|每个通道|都改成|都调整|统一降低|统一提高|通道.*都",
            t
        ))

    async def _handle_batch_modification(self, text: str, user_id: str, context: Dict) -> str:
        data = self.config.get_exchange_rates_config()
        channels = data.get("channels") or {}
        if not channels:
            return "未找到通道配置。"

        m_rate = re.search(
            r"(?:所有|全部|每个)通道.*?(?:费率|汇率).*?(?:统一|都)?(?:改为|改成|设为|调到|调整为)\s*([\d.]+%?)",
            text
        )
        m_delta = re.search(
            r"(?:所有|全部|每个)通道.*?(?:费率|汇率).*?(降低|提高|增加|减少)\s*([\d.]+)%?",
            text
        )

        if m_rate:
            new_rate = m_rate.group(1)
            preview_lines = ["批量修改预览 — 所有通道费率（代收+代付）\n"]
            for key, cfg in channels.items():
                old_pi = self._dir_get(cfg, "payin", "fee_rate") or "-"
                old_po = self._dir_get(cfg, "payout", "fee_rate") or "-"
                old = old_pi if old_pi == old_po else f"代收{old_pi}/代付{old_po}"
                preview_lines.append(f"  {cfg.get('display_name', key)}: {old} → {new_rate}")
            preview_lines.append(f"\n共 {len(channels)} 个通道将被修改。")
            preview_lines.append("请回复「确认批量修改」执行，或忽略取消。")

            ctx = context.get("user_context", {})
            ctx["batch_pending"] = {
                "type": "set_all_rate", "rate": new_rate,
                "channel_keys": list(channels.keys()),
            }
            return "\n".join(preview_lines)

        if m_delta:
            direction = m_delta.group(1)
            delta = float(m_delta.group(2))
            if direction in ("降低", "减少"):
                delta = -delta
            preview_lines = ["批量修改预览 — 所有通道费率（代收+代付）\n"]
            new_rates = {}
            for key, cfg in channels.items():
                old_str = self._dir_get(cfg, "payin", "fee_rate") or "0%"
                old_num = float(re.sub(r"[^\d.]", "", old_str) or "0")
                new_num = max(0, old_num + delta)
                new_str = f"{new_num}%"
                new_rates[key] = new_str
                preview_lines.append(f"  {cfg.get('display_name', key)}: {old_str} → {new_str}")
            preview_lines.append(f"\n共 {len(channels)} 个通道将被修改。")
            preview_lines.append("请回复「确认批量修改」执行，或忽略取消。")

            ctx = context.get("user_context", {})
            ctx["batch_pending"] = {
                "type": "delta_rate", "rates": new_rates,
                "channel_keys": list(channels.keys()),
            }
            return "\n".join(preview_lines)

        return None

    async def _execute_batch(self, user_id: str, batch: dict) -> str:
        data = self.config.get_exchange_rates_config()
        channels = data.get("channels") or {}
        snap = self._save_snapshot("exchange_rates")
        changed = []

        if batch["type"] == "set_all_rate":
            rate = batch["rate"]
            for key in batch["channel_keys"]:
                ch = channels.get(key)
                if not ch:
                    continue
                old_parts = []
                for d in ("payin", "payout"):
                    sub = ch.get(d)
                    if isinstance(sub, dict):
                        old_parts.append(sub.get("fee_rate", "-"))
                        sub["fee_rate"] = rate
                if not old_parts:
                    old_parts.append(ch.get("fee_rate", "-"))
                    ch["fee_rate"] = rate
                ch["last_updated"] = time.strftime("%Y-%m-%d")
                changed.append(f"{ch.get('display_name', key)}: {'/'.join(old_parts)} → {rate}")

        elif batch["type"] == "delta_rate":
            for key, new_rate in batch["rates"].items():
                ch = channels.get(key)
                if not ch:
                    continue
                old_parts = []
                for d in ("payin", "payout"):
                    sub = ch.get(d)
                    if isinstance(sub, dict):
                        old_parts.append(sub.get("fee_rate", "-"))
                        sub["fee_rate"] = new_rate
                if not old_parts:
                    old_parts.append(ch.get("fee_rate", "-"))
                    ch["fee_rate"] = new_rate
                ch["last_updated"] = time.strftime("%Y-%m-%d")
                changed.append(f"{ch.get('display_name', key)}: {'/'.join(old_parts)} → {new_rate}")

        data["channels"] = channels
        ok, msg = self.config.save_exchange_rates(data)
        if not ok:
            return f"批量保存失败：{msg}"
        self._log_audit(user_id, "batch_update_rate",
                        f"{len(changed)} channels", "", ", ".join(changed)[:200], snap)
        return f"已批量更新 {len(changed)} 个通道费率\n" + "\n".join(f"  {c}" for c in changed)

    # ── 帮助 ───────────────────────────────────────────────────

    def _get_help_message(self) -> str:
        return ("配置管理命令：\n\n"
                "特殊群/黑名单：\n"
                "  列出特殊群 / 添加特殊群 <群名> / 删除特殊群 <群名>\n"
                "  列出黑名单 / 添加黑名单 <群名> / 删除黑名单 <群名>\n\n"
                "话术模板：\n"
                "  列出话术 / 查看话术 <名称>\n"
                "  更新话术 <名称> <内容> / 添加话术 <名称> <内容>\n"
                "  删除话术 <名称> <序号>\n\n"
                "通道管理：\n"
                "  列出通道 / 查看通道 <名称>\n"
                "  更新费率 <名称> <费率> / 更新额度 <名称> <范围>\n"
                "  启用通道 <名称> / 禁用通道 <名称>\n\n"
                "配置查看 & 审计：\n"
                "  查看配置 / 查看话术 / 查看通道\n"
                "  查看操作记录 / 列出快照\n"
                "  撤销上次 / 回滚 <快照ID>")
