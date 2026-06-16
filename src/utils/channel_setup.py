"""渠道接入向导规格（P1-1）— 声明式「每渠道需要填什么 + 现状如何」。

把「接入一个渠道要填哪些字段、去哪拿、填了没、对不对」收敛成一份**声明式规格**，
供 Web 向导前端渲染表单、后端按渠道写凭证 overlay、并复用 P0-1 ``check_config``
给出即时校验。零依赖、纯函数，便于单测。

设计：每个渠道 = 若干 :class:`Field`（点分 config 路径 + 标签 + 是否密钥/必填 + 取得指引）。
``channel_status(config)`` 返回每渠道填写/校验现状；``apply_channel_values(overlay,
channel, values)`` 只接受声明字段、按类型强转后写入 overlay dict（防注入任意键）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# 占位符判定（与 config_check 同源思路）：空、YOUR_*、<...>、changeme 等视为未填。
_PLACEHOLDER_TOKENS = ("your_", "<", "changeme", "xxxx", "请填写", "填写", "placeholder")


def _is_placeholder(val: Any) -> bool:
    s = str(val if val is not None else "").strip()
    if not s:
        return True
    low = s.lower()
    return any(tok in low for tok in _PLACEHOLDER_TOKENS)


@dataclass
class Field:
    key: str                       # 点分 config 路径，如 "telegram.api_id"
    label: str
    required: bool = True
    secret: bool = False           # 密钥类：状态接口回显时打码
    type: str = "str"             # str | int | bool
    help: str = ""                 # 去哪拿 / 填什么


@dataclass
class Channel:
    id: str
    name: str
    enable_key: str                # 启用开关的 config 路径
    fields: List[Field] = field(default_factory=list)
    login_required: bool = False   # 填完后是否还需扫码/登录（交棒现有登录流程）
    intro: str = ""


CHANNELS: List[Channel] = [
    Channel(
        id="telegram",
        name="Telegram",
        enable_key="telegram.enabled",
        login_required=True,
        intro="填入 API 凭证后，回到账号页扫码 / 验证码登录账号。",
        fields=[
            Field("telegram.api_id", "API ID", type="int",
                  help="https://my.telegram.org → API development tools 获取"),
            Field("telegram.api_hash", "API Hash", secret=True,
                  help="同 my.telegram.org 页面，与 API ID 配对"),
            Field("telegram.phone_number", "手机号", required=False,
                  help="可选；登录账号时也可现填，格式 +8613800000000"),
        ],
    ),
    Channel(
        id="line",
        name="LINE 官方账号",
        enable_key="line.enabled",
        intro="LINE Developers 控制台 → Messaging API 频道获取以下凭证。",
        fields=[
            Field("line.channel_access_token", "Channel Access Token", secret=True,
                  help="Messaging API 设置页签发的长期 token"),
            Field("line.channel_secret", "Channel Secret", secret=True,
                  help="频道基本设置页的 Channel secret"),
        ],
    ),
    Channel(
        id="messenger",
        name="Facebook Messenger",
        enable_key="facebook_messenger.enabled",
        intro="Meta 开发者后台 → 你的 App → Messenger 产品获取以下凭证。",
        fields=[
            Field("facebook_messenger.page_access_token", "Page Access Token", secret=True,
                  help="绑定主页后签发的 Page token"),
            Field("facebook_messenger.verify_token", "Verify Token",
                  help="自定义任意字符串，需与 Webhook 配置一致"),
            Field("facebook_messenger.app_secret", "App Secret", secret=True, required=False,
                  help="可选；用于校验 Webhook 签名（X-Hub-Signature）"),
        ],
    ),
    Channel(
        id="web",
        name="网页客服 Widget",
        enable_key="web_chat.enabled",
        intro="服务端原生渠道，无需第三方凭证，开启即用。",
        fields=[],
    ),
]

_CHANNEL_BY_ID: Dict[str, Channel] = {c.id: c for c in CHANNELS}


def _dig(config: Dict[str, Any], dotted: str) -> Any:
    cur: Any = config
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _mask(val: Any) -> str:
    """密钥回显打码：保留首尾各 2 位。"""
    s = str(val if val is not None else "")
    if len(s) <= 4:
        return "•" * len(s)
    return s[:2] + "•" * max(3, len(s) - 4) + s[-2:]


def get_channel(channel_id: str) -> Optional[Channel]:
    return _CHANNEL_BY_ID.get(str(channel_id or "").lower())


def channel_status(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """每渠道接入现状：是否启用 / 必填是否齐 / 各字段填写状态（密钥打码）。"""
    config = config or {}
    out: List[Dict[str, Any]] = []
    for ch in CHANNELS:
        enabled = bool(_dig(config, ch.enable_key))
        fields_status = []
        missing: List[str] = []
        for fld in ch.fields:
            raw = _dig(config, fld.key)
            filled = not _is_placeholder(raw)
            if fld.required and not filled:
                missing.append(fld.label)
            disp = ""
            if filled:
                disp = _mask(raw) if fld.secret else str(raw)
            fields_status.append({
                "key": fld.key, "label": fld.label, "required": fld.required,
                "secret": fld.secret, "type": fld.type, "help": fld.help,
                "filled": filled, "display": disp,
            })
        configured = (not missing) and (bool(ch.fields) or enabled)
        out.append({
            "id": ch.id, "name": ch.name, "enable_key": ch.enable_key,
            "enabled": enabled, "intro": ch.intro,
            "login_required": ch.login_required,
            "fields": fields_status, "missing": missing,
            "configured": configured,
            "ready": configured and enabled,
        })
    return out


def _coerce(value: Any, typ: str) -> Any:
    if typ == "int":
        return int(str(value).strip())
    if typ == "bool":
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    return str(value)


def _set_dotted(target: Dict[str, Any], dotted: str, value: Any) -> None:
    cur = target
    parts = dotted.split(".")
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value


def apply_channel_values(
    overlay: Dict[str, Any], channel: str, values: Dict[str, Any],
) -> Tuple[bool, str]:
    """把 values 中的**已声明字段**写入 overlay（就地），并自动置 enabled=true。

    - 只认 channel 规格里的 field.key（其它键忽略，防注入任意配置）；
    - 空值跳过（不覆盖已有）；类型按 field.type 强转，转换失败即报错；
    - 写入任一字段后顺带把该渠道 enable_key 设为 true。
    返回 (成功?, 说明)。
    """
    ch = get_channel(channel)
    if ch is None:
        return False, f"未知渠道: {channel}"
    values = values or {}
    by_key = {f.key: f for f in ch.fields}
    # 兼容前端用短键（api_id）或全路径（telegram.api_id）
    short_map = {f.key.split(".")[-1]: f for f in ch.fields}
    wrote = False
    for raw_key, raw_val in values.items():
        fld = by_key.get(raw_key) or short_map.get(raw_key)
        if fld is None:
            continue
        if _is_placeholder(raw_val):
            continue
        try:
            coerced = _coerce(raw_val, fld.type)
        except Exception:
            return False, f"字段 {fld.label} 值无效（应为 {fld.type}）"
        _set_dotted(overlay, fld.key, coerced)
        wrote = True
    # web 等无字段渠道：仅开启
    if wrote or not ch.fields:
        _set_dotted(overlay, ch.enable_key, True)
    return True, "ok"
