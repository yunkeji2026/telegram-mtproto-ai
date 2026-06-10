"""平台扫码登录会话管理（P3）。

统一收件箱「账号管理 → ＋ 扫码新增 / 重连」对应的后端。本模块**不直接驱动**各
平台底层登录（LINE/WhatsApp/Messenger 为手机设备 RPA，登录二维码显示在被控设备/
投屏端；Telegram 走 pyrogram 多账号），而是提供一个**安全、可插拔**的登录会话层：

- ``LoginManager``：内存登录会话（带 TTL），记录发起时该平台「已在线账号」基线。
- 轮询时通过适配器的实时状态对比基线，**检测到新账号上线即判定 authorized**——
  即无论扫码在设备端还是网页端完成，账号真正连上后前端弹窗会自动转「登录成功」。
- ``register_login_provider``：预留真实 per-platform QR provider 的注册点（如未来为
  Telegram 接入 pyrogram ExportLoginToken 网页二维码），核心轮询逻辑无需改动。

设计原则：只读各服务的 ``status()``，**绝不触碰正在运行的客户端**，零副作用。
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Set

# 登录会话有效期（秒）：二维码/等待窗口超过即过期，前端可「刷新」重开
TTL_SEC = 180

SUPPORTED_PLATFORMS = ("telegram", "line", "whatsapp", "messenger", "web")

# 登录方式（mode）：协议多开 / 网页隔离 / 真机RPA
MODES = ("protocol", "web", "device")
MODE_LABELS: Dict[str, str] = {
    "protocol": "协议多开",
    "web": "网页扫码",
    "device": "真机 / 模拟器",
}
MODE_DESC: Dict[str, str] = {
    "protocol": "服务端协议直连，单机可挂大量账号，最省资源（推荐）",
    "web": "隔离浏览器 + 平台网页二维码，兼容好、更像真人",
    "device": "真机 / 模拟器扫码，最难封号，账号数受设备数限制",
}

# 每平台默认可选方式与默认方式（可被 config.platform_login.<platform> 覆盖）
DEFAULT_PLATFORM_MODES: Dict[str, Dict[str, Any]] = {
    "telegram": {"modes": ["protocol", "web", "device"], "default": "protocol"},
    "whatsapp": {"modes": ["protocol", "web", "device"], "default": "protocol"},
    "line": {"modes": ["device"], "default": "device"},
    "messenger": {"modes": ["web", "device"], "default": "web"},
    "web": {"modes": [], "default": ""},
}

# 各平台扫码指引（设备/投屏端为主，网页端 provider 可覆盖）
PLATFORM_INSTRUCTIONS: Dict[str, str] = {
    "telegram": (
        "在 Telegram 手机端：设置 → 设备 → 关联桌面设备，扫描二维码；"
        "或由管理员在设备端完成新账号登录。账号连上后本窗口会自动确认。"
    ),
    "line": (
        "在已连接的设备 / 投屏端打开 LINE 登录页并用手机扫码；"
        "登录成功后本窗口会自动确认。"
    ),
    "whatsapp": (
        "在设备 / 投屏端打开 WhatsApp「关联设备」并用手机扫码；"
        "成功后本窗口会自动确认。"
    ),
    "messenger": (
        "在设备 / 投屏端完成 Facebook 账号登录授权；成功后本窗口会自动确认。"
    ),
    "web": "网页客服为服务端原生渠道，无需扫码登录。",
}


@dataclass
class LoginSession:
    login_id: str
    platform: str
    mode: str = "device"
    account_id: str = ""
    created_at: float = field(default_factory=time.time)
    status: str = "pending"  # pending | scanned | authorized | expired | failed
    qr_url: str = ""          # 网页二维码可编码的 URL（如 tg://login?token=...）
    qr_image: str = ""        # data URI（可选，provider 直接给出二维码图片）
    instruction: str = ""
    detail: str = ""
    baseline: Set[str] = field(default_factory=set)
    # M4：账号配置（防关联）—— 登录成功后随 account_id 一起落库
    label: str = ""
    group: str = ""
    proxy_id: str = ""
    fingerprint_id: str = ""
    # provider 事件驱动钩子（protocol/web provider 用；device 走基线轮询，留空）
    provider_state: Any = None
    poll_fn: Optional[Callable[..., Any]] = None
    cancel_fn: Optional[Callable[..., Any]] = None

    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > TTL_SEC


class LoginManager:
    """内存登录会话表（进程内，单实例）。线程安全。"""

    def __init__(self) -> None:
        self._sessions: Dict[str, LoginSession] = {}
        self._lock = threading.Lock()

    def _gc_locked(self) -> None:
        now = time.time()
        dead = [
            k for k, s in self._sessions.items()
            if (now - s.created_at) > TTL_SEC * 2
        ]
        for k in dead:
            self._sessions.pop(k, None)

    def create(
        self,
        platform: str,
        account_id: str,
        baseline: Set[str],
        *,
        mode: str = "device",
        qr_url: str = "",
        qr_image: str = "",
        instruction: str = "",
        label: str = "",
        group: str = "",
        proxy_id: str = "",
        fingerprint_id: str = "",
        provider_state: Any = None,
        poll_fn: Optional[Callable[..., Any]] = None,
        cancel_fn: Optional[Callable[..., Any]] = None,
    ) -> LoginSession:
        with self._lock:
            self._gc_locked()
            sid = secrets.token_urlsafe(12)
            sess = LoginSession(
                login_id=sid,
                platform=platform,
                mode=mode or "device",
                account_id=account_id or "",
                baseline=set(baseline or set()),
                qr_url=qr_url,
                qr_image=qr_image,
                instruction=instruction
                or PLATFORM_INSTRUCTIONS.get(platform, "请完成扫码登录。"),
                label=label,
                group=group,
                proxy_id=proxy_id,
                fingerprint_id=fingerprint_id,
                provider_state=provider_state,
                poll_fn=poll_fn,
                cancel_fn=cancel_fn,
            )
            self._sessions[sid] = sess
            return sess

    def get(self, login_id: str) -> Optional[LoginSession]:
        with self._lock:
            return self._sessions.get(login_id)

    def cancel(self, login_id: str) -> None:
        with self._lock:
            self._sessions.pop(login_id, None)


_manager: Optional[LoginManager] = None


def get_login_manager() -> LoginManager:
    global _manager
    if _manager is None:
        _manager = LoginManager()
    return _manager


def online_account_keys(
    status_map: Dict[str, Dict[str, Any]], platform: str
) -> Set[str]:
    """从 ``status_via_adapters`` 结果中取某平台「在线」账号的唯一键集合。"""
    keys: Set[str] = set()
    for k, v in (status_map or {}).items():
        if not isinstance(v, dict):
            continue
        if (v.get("platform") or "") != platform:
            continue
        if v.get("running"):
            keys.add(str(v.get("account_id") or k))
    return keys


# ── per-(platform, mode) QR provider 注册点 ──────────────────────────────────
# provider(request, platform, mode, account_id) -> dict|None
#   返回 {"qr_url"?, "qr_image"?, "instruction"?, "account_id"?} 覆盖默认指引；
#   返回 None 表示沿用「设备端扫码 + 状态轮询」。
#   key = f"{platform}:{mode}"。M2/M3 起为 telegram:protocol / whatsapp:protocol 注册真实 provider。
_PROVIDERS: Dict[str, Callable[..., Optional[Dict[str, Any]]]] = {}


def _pkey(platform: str, mode: str) -> str:
    return f"{str(platform).lower()}:{str(mode).lower()}"


def register_login_provider(
    platform: str, mode: str, provider: Callable[..., Optional[Dict[str, Any]]]
) -> None:
    _PROVIDERS[_pkey(platform, mode)] = provider


def get_login_provider(
    platform: str, mode: str = "device",
) -> Optional[Callable[..., Optional[Dict[str, Any]]]]:
    return _PROVIDERS.get(_pkey(platform, mode))


def mode_available(platform: str, mode: str) -> bool:
    """该平台该方式是否可用。device 为内置（设备端扫码+状态轮询）始终可用；
    protocol/web 需注册了真实 provider 才可用。"""
    mode = str(mode).lower()
    if mode == "device":
        return True
    return get_login_provider(platform, mode) is not None


def list_modes(
    platform: str, platform_cfg: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """返回该平台的登录方式清单（供前端「选择登录方式」渲染）。

    ``platform_cfg`` 为 config.platform_login.<platform>（可选，覆盖默认 modes/default）。
    """
    platform = str(platform or "").lower()
    pdef = DEFAULT_PLATFORM_MODES.get(
        platform, {"modes": ["device"], "default": "device"}
    )
    cfg = platform_cfg or {}
    modes = cfg.get("modes") or pdef["modes"]
    default = cfg.get("default") or pdef["default"]
    out: List[Dict[str, Any]] = []
    for m in modes:
        avail = mode_available(platform, m)
        out.append({
            "mode": m,
            "label": MODE_LABELS.get(m, m),
            "desc": MODE_DESC.get(m, ""),
            "available": avail,
            "recommended": (m == default),
            "reason": "" if avail else "开发中 / 未启用",
        })
    return out
