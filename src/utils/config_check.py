"""配置自检（P0-1）— 规则驱动的 config.yaml 体检。

不依赖 Pydantic：对 1190 行深嵌套配置做全量建模成本高、易随新字段 break 启动路径。
本模块聚焦**可操作**的错配拦截：

- 占位符未替换（``YOUR_API_KEY`` / 空串）
- ``enabled: true`` 的子系统缺必填项（如开了 LINE 但 channel_secret 空）
- 已知 footgun（如 ``ai.provider: deepseek`` 会静默回落 gemini）
- 跨字段一致性（如 web_chat 引流需 contacts.enabled）
- 关键数值合法性（max_tokens>0、sla_warn<sla_crit…）

每条产出 :class:`Issue`（severity + dotted path + message + hint），由
:func:`check_config` 汇总。``error`` 阻断；``warn``/``info`` 仅提示。

入口：``check_config(config, config_path=...) -> list[Issue]``；
渲染：``format_report(issues, config=...) -> str``。两者均为纯函数，便于测试与
被 ``main.py --check`` / ConfigManager 启动自检复用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# AIClient 实际只实现这两条分支（见 config.example.yaml::ai 注释）
_VALID_AI_PROVIDERS = {"gemini", "openai_compatible"}
_PLACEHOLDER_RE = re.compile(r"^(YOUR_|<.*>$|xxx+$)", re.IGNORECASE)

ERROR = "error"
WARN = "warn"
INFO = "info"
_SEVERITY_ORDER = {ERROR: 0, WARN: 1, INFO: 2}


@dataclass
class Issue:
    """单条自检结论。"""

    severity: str
    path: str
    message: str
    hint: str = ""

    def __str__(self) -> str:  # pragma: no cover - 渲染细节由 format_report 测
        tag = {ERROR: "✗", WARN: "!", INFO: "·"}.get(self.severity, "?")
        base = f"[{tag}] {self.path}: {self.message}"
        return f"{base}\n      → {self.hint}" if self.hint else base


@dataclass
class _Ctx:
    """检查上下文：根 config + 解析相对路径用的 config 目录 + 累积的 issues。"""

    config: Dict[str, Any]
    config_dir: Optional[Path] = None
    issues: List[Issue] = field(default_factory=list)

    def add(self, severity: str, path: str, message: str, hint: str = "") -> None:
        self.issues.append(Issue(severity, path, message, hint))

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.config
        for k in dotted.split("."):
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    def section(self, name: str) -> Dict[str, Any]:
        val = self.config.get(name)
        return val if isinstance(val, dict) else {}

    def enabled(self, dotted: str) -> bool:
        return bool(self.get(f"{dotted}.enabled", False)) if "." in dotted else bool(
            self.section(dotted).get("enabled", False))


def _is_placeholder(value: Any) -> bool:
    """空串 / 仅空白 / YOUR_xxx / <...> / xxxx 视为未填占位符。"""
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    s = value.strip()
    if not s:
        return True
    return bool(_PLACEHOLDER_RE.match(s))


def _require(ctx: _Ctx, dotted: str, *, when: str, hint: str = "", severity: str = ERROR) -> bool:
    """要求某字段非占位符；缺失则记一条 issue。返回是否通过。"""
    if _is_placeholder(ctx.get(dotted)):
        ctx.add(severity, dotted, f"{when}，但未填写（当前为空或占位符）", hint)
        return False
    return True


# ── 各子系统检查（每个函数只看自己关心的段，互不依赖）────────────────────

def _check_ai(ctx: _Ctx) -> None:
    ai = ctx.section("ai")
    if not ai:
        ctx.add(ERROR, "ai", "缺少 ai 配置段", "至少需配置 provider / api_key / model")
        return
    provider = str(ai.get("provider") or "").strip()
    if not provider:
        ctx.add(ERROR, "ai.provider", "未指定 AI 提供方", "填 gemini 或 openai_compatible")
    elif provider not in _VALID_AI_PROVIDERS:
        ctx.add(
            ERROR, "ai.provider",
            f"provider='{provider}' 不是受支持的分支，会静默回落到 gemini",
            "AIClient 仅实现 gemini / openai_compatible；用 DeepSeek/Ollama 须写 "
            "openai_compatible + base_url",
        )
    if provider == "openai_compatible":
        _require(ctx, "ai.base_url", when="provider=openai_compatible 需要 base_url",
                 hint="如 DeepSeek: https://api.deepseek.com；Ollama: http://127.0.0.1:11434/v1")
    _require(ctx, "ai.api_key", when="AI 调用需要 api_key",
             hint="本地 Ollama 可填任意非空值（如 ollama）", severity=WARN)
    _require(ctx, "ai.model", when="需指定模型名", hint="如 deepseek-chat / gemini-2.0-flash")

    # 数值合法性
    _check_positive_int(ctx, "ai.max_tokens", ai.get("max_tokens"))
    _check_positive_int(ctx, "ai.timeout", ai.get("timeout"))
    temp = ai.get("temperature")
    if temp is not None:
        try:
            t = float(temp)
            if not (0.0 <= t <= 2.0):
                ctx.add(WARN, "ai.temperature", f"temperature={temp} 超出常规区间 0.0–2.0")
        except (TypeError, ValueError):
            ctx.add(WARN, "ai.temperature", f"temperature 不是有效数字: {temp!r}")


def _check_positive_int(ctx: _Ctx, path: str, value: Any) -> None:
    if value is None:
        return
    try:
        if int(value) <= 0:
            ctx.add(WARN, path, f"应为正整数，当前 {value!r}")
    except (TypeError, ValueError):
        ctx.add(WARN, path, f"不是有效整数: {value!r}")


def _check_telegram(ctx: _Ctx) -> None:
    tg = ctx.section("telegram")
    if not tg:
        return  # telegram 可选（纯 web_chat / 纯 RPA 部署）
    # 「是否打算用 TG」只看 api_id/api_hash（决定性凭证）；手机号单独存在无法工作，
    # 故不据它判定——避免照抄 example 假手机号的 web_chat-only 用户被误报。
    creds = ("api_id", "api_hash")
    touched = any(not _is_placeholder(tg.get(k)) for k in creds)
    if not touched:
        ctx.add(INFO, "telegram", "Telegram 凭证为占位符，TG 渠道将不可用（若不用 TG 可忽略）")
        return
    for k in ("api_id", "api_hash", "phone_number"):
        _require(ctx, f"telegram.{k}", when="Telegram 渠道已配置",
                 hint="从 https://my.telegram.org 获取")


def _check_line(ctx: _Ctx) -> None:
    if not ctx.enabled("line"):
        return
    _require(ctx, "line.channel_secret", when="LINE 官方渠道已启用")
    _require(ctx, "line.channel_access_token", when="LINE 官方渠道已启用")


def _check_web_chat(ctx: _Ctx) -> None:
    if not ctx.enabled("web_chat"):
        return
    if _is_placeholder(ctx.get("web_chat.token_secret")) and _is_placeholder(
        ctx.get("web_admin.secret_key")
    ):
        ctx.add(WARN, "web_chat.token_secret",
                "访客 token 签名密钥为空，且回退的 web_admin.secret_key 也未设置",
                "设置 web_chat.token_secret 或 web_admin.secret_key（防 token 伪造）")
    if ctx.get("web_chat.handoff.enabled") and not ctx.enabled("contacts"):
        ctx.add(ERROR, "web_chat.handoff.enabled",
                "web→LINE 引流依赖 contacts 子系统，但 contacts.enabled=false",
                "开启 contacts.enabled 或关闭 web_chat.handoff.enabled")


def _check_web_admin(ctx: _Ctx) -> None:
    wa = ctx.section("web_admin")
    if not wa:
        return
    if _is_placeholder(wa.get("secret_key")):
        ctx.add(WARN, "web_admin.secret_key", "后台会话密钥为占位符",
                "设置随机长字符串（影响登录会话与多处 token 签名安全）")
    if _is_placeholder(wa.get("auth_token")):
        ctx.add(WARN, "web_admin.auth_token", "后台 auth_token 为占位符")


def _check_messenger_rpa(ctx: _Ctx) -> None:
    if not ctx.enabled("messenger_rpa"):
        return
    mr = ctx.section("messenger_rpa")
    accounts = mr.get("accounts") or []
    if not accounts and _is_placeholder(mr.get("adb_serial")):
        ctx.add(WARN, "messenger_rpa", "已启用但既无 accounts[] 也无 adb_serial",
                "单账号填 adb_serial，多账号填 accounts[].adb_serial")


def _check_whatsapp_rpa(ctx: _Ctx) -> None:
    if not ctx.enabled("whatsapp_rpa"):
        return
    if not (ctx.section("whatsapp_rpa").get("accounts") or []):
        ctx.add(WARN, "whatsapp_rpa.accounts", "已启用但 accounts[] 为空",
                "为每台手机填一条 {account_id, adb_serial}")


def _check_device_coordinator(ctx: _Ctx) -> None:
    if not ctx.enabled("device_coordinator"):
        return
    if not (ctx.section("device_coordinator").get("devices") or []):
        ctx.add(WARN, "device_coordinator.devices", "已启用但 devices[] 为空")


def _check_webhook(ctx: _Ctx) -> None:
    if not ctx.enabled("webhook"):
        return
    if not (ctx.section("webhook").get("webhooks") or []):
        ctx.add(WARN, "webhook.webhooks", "告警 webhook 已启用但未配置任何 url")


def _check_contacts(ctx: _Ctx) -> None:
    if not ctx.enabled("contacts"):
        return
    for key in ("scripts_path", "compliance_path"):
        rel = ctx.get(f"contacts.{key}")
        if _is_placeholder(rel):
            continue
        if ctx.config_dir is not None:
            p = Path(rel)
            if not p.is_absolute():
                p = ctx.config_dir / p
            if not p.exists():
                ctx.add(WARN, f"contacts.{key}", f"引流话术文件不存在: {p}")


def _check_ecommerce(ctx: _Ctx) -> None:
    if not ctx.enabled("ecommerce_tools"):
        return
    provider = str(ctx.get("ecommerce_tools.provider") or "mock").strip()
    if provider == "shopify":
        _require(ctx, "ecommerce_tools.shopify.shop", when="ecommerce provider=shopify",
                 severity=WARN, hint="缺 shop/token 会自动回落 mock")
        _require(ctx, "ecommerce_tools.shopify.access_token",
                 when="ecommerce provider=shopify", severity=WARN)


def _check_translation(ctx: _Ctx) -> None:
    engines = ctx.get("translation.engines") or {}
    order = engines.get("order") or []
    if "deepl" in order and _is_placeholder((engines.get("deepl") or {}).get("api_key")):
        ctx.add(WARN, "translation.engines.deepl.api_key",
                "引擎顺序含 deepl 但未配 api_key，将被自动跳过")
    if "google" in order and _is_placeholder((engines.get("google") or {}).get("api_key")):
        ctx.add(WARN, "translation.engines.google.api_key",
                "引擎顺序含 google 但未配 api_key，将被自动跳过")


def _check_voice_recognition(ctx: _Ctx) -> None:
    if not ctx.enabled("voice_recognition"):
        return
    if str(ctx.get("voice_recognition.provider") or "").strip() == "openai":
        _require(ctx, "voice_recognition.openai.api_key",
                 when="voice_recognition provider=openai", severity=WARN)


def _check_workspace(ctx: _Ctx) -> None:
    if ctx.get("workspace.auto_assign.auto_claim.enabled") and not ctx.get(
        "workspace.auto_assign.enabled"
    ):
        ctx.add(WARN, "workspace.auto_assign.auto_claim.enabled",
                "自动认领已开，但 auto_assign.enabled=false（决策不会产生）",
                "同时开启 workspace.auto_assign.enabled")


def _check_inbox(ctx: _Ctx) -> None:
    warn = ctx.get("inbox.sla_warn_sec")
    crit = ctx.get("inbox.sla_crit_sec")
    try:
        if warn is not None and crit is not None and int(warn) >= int(crit):
            ctx.add(WARN, "inbox.sla_crit_sec",
                    f"严重阈值({crit}) 应大于警告阈值({warn})")
    except (TypeError, ValueError):
        pass


_CHECKS: List[Callable[[_Ctx], None]] = [
    _check_ai,
    _check_telegram,
    _check_line,
    _check_web_chat,
    _check_web_admin,
    _check_messenger_rpa,
    _check_whatsapp_rpa,
    _check_device_coordinator,
    _check_webhook,
    _check_contacts,
    _check_ecommerce,
    _check_translation,
    _check_voice_recognition,
    _check_workspace,
    _check_inbox,
]


def check_config(config: Dict[str, Any], *, config_path: Any = None) -> List[Issue]:
    """对配置 dict 跑全部规则，返回 issue 列表（按严重度排序）。"""
    config_dir: Optional[Path] = None
    if config_path:
        try:
            config_dir = Path(config_path).parent
        except Exception:
            config_dir = None
    ctx = _Ctx(config=config if isinstance(config, dict) else {}, config_dir=config_dir)
    if not isinstance(config, dict):
        ctx.add(ERROR, "<root>", "配置不是有效的 YAML 字典")
        return ctx.issues
    for check in _CHECKS:
        try:
            check(ctx)
        except Exception as exc:  # 单条规则异常不应拖垮整个自检
            ctx.add(INFO, "<checker>", f"规则 {check.__name__} 执行异常: {exc}")
    ctx.issues.sort(key=lambda i: (_SEVERITY_ORDER.get(i.severity, 9), i.path))
    return ctx.issues


def _enabled_subsystems(config: Dict[str, Any]) -> List[str]:
    """列出所有 ``enabled: true`` 的顶层子系统（用于就绪报告概览）。"""
    out = []
    for name, val in (config or {}).items():
        if isinstance(val, dict) and val.get("enabled") is True:
            out.append(name)
    return sorted(out)


def format_report(issues: List[Issue], *, config: Optional[Dict[str, Any]] = None) -> str:
    """把 issues 渲染成可读的控制台报告。"""
    lines: List[str] = []
    lines.append("=" * 64)
    lines.append("配置自检报告 (config check)")
    lines.append("=" * 64)

    if config is not None:
        subs = _enabled_subsystems(config)
        lines.append(f"已启用子系统: {', '.join(subs) if subs else '（无）'}")
        lines.append("-" * 64)

    errors = [i for i in issues if i.severity == ERROR]
    warns = [i for i in issues if i.severity == WARN]
    infos = [i for i in issues if i.severity == INFO]

    for label, group in (("错误", errors), ("警告", warns), ("提示", infos)):
        if not group:
            continue
        lines.append(f"{label} ({len(group)}):")
        for i in group:
            lines.append("  " + str(i).replace("\n", "\n  "))
        lines.append("")

    if not issues:
        lines.append("✓ 未发现问题。")
    else:
        lines.append(
            f"汇总: {len(errors)} 错误 / {len(warns)} 警告 / {len(infos)} 提示")
    lines.append("=" * 64)
    return "\n".join(lines)


def has_errors(issues: List[Issue]) -> bool:
    return any(i.severity == ERROR for i in issues)
