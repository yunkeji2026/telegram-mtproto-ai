"""N 线 核心4：统一运行时——让协议号跑 A 线"有灵魂"的 TelegramClient。

收敛 A/B 两线的命门：
- **B 线**（扫码/协议登录）把 session 落到 ``sessions/<session_name>.session``，并登记
  ``account_registry(mode=protocol, meta.session_name)``；其默认 worker
  ``TelegramProtocolWorker`` 只起一个**精简** pyrogram 连接（收消息→收件箱/简单 autoreply）。
- **A 线**（``src/client/telegram_client.py::TelegramClient``）是"有灵魂"的丰富 client：
  记忆/人设/情绪增强/语音图片识别/四层触发/人工转接/GXP/定时任务……但原本只能由
  ``main.py`` 按 config 单/多账号拉起。

本 worker 把两者打通：用 B 线落盘的 ``session_name`` 直接拉起 **A 线 TelegramClient**
（session 已授权，无需 phone），从而"扫码登录的号"也获得完整陪聊能力。

零破坏约定：
- 默认关（``platform_login.telegram.companion_runtime: false``）。关时编排器仍用既有
  ``TelegramProtocolWorker``（B 线薄连接），行为不变。
- A 线 client 需要 ``config_manager`` + ``skill_manager``（+ 可选 ``ai_client``），而编排器
  只有 config dict。故由 app 启动时经 ``set_companion_context`` 注入一份进程级运行时上下文，
  本 worker 在 ``start()`` 时读取。上下文未就绪 → 启动报错（被编排器退避兜住），不影响主进程。
- pyrogram / TelegramClient 全程**惰性导入**（在 ``start()`` 内），模块导入零重依赖、可单测。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── 进程级运行时上下文（app 启动时注入；worker 读取以构建 A 线 client） ──────────

_CTX: Dict[str, Any] = {
    "config_manager": None,
    "skill_manager": None,
    "ai_client": None,
}


def set_companion_context(
    *, config_manager: Any, skill_manager: Any, ai_client: Any = None
) -> None:
    """app 启动时注入构建 A 线 client 所需的依赖（幂等，可重复覆盖）。"""
    _CTX["config_manager"] = config_manager
    _CTX["skill_manager"] = skill_manager
    _CTX["ai_client"] = ai_client


def get_companion_context() -> Dict[str, Any]:
    return dict(_CTX)


def companion_context_ready() -> bool:
    return _CTX.get("config_manager") is not None and _CTX.get("skill_manager") is not None


def reset_companion_context() -> None:
    """测试钩子：清空注入的上下文。"""
    _CTX["config_manager"] = None
    _CTX["skill_manager"] = None
    _CTX["ai_client"] = None


# ── feature flag ─────────────────────────────────────────────────────────────

def companion_runtime_enabled(config: Optional[Dict[str, Any]]) -> bool:
    """是否让协议号走 A 线丰富运行时（默认关）。"""
    pl = (config or {}).get("platform_login", {}) or {}
    tg = pl.get("telegram", {}) or {}
    return bool(tg.get("companion_runtime", False))


# ── worker ───────────────────────────────────────────────────────────────────

class TelegramCompanionWorker:
    """用 B 线落盘 session 拉起 A 线 TelegramClient，并实现编排器 worker 协议。

    worker 协议：``async start()/stop()``、``async healthy()->bool``、``status()->dict``，
    可选 ``async send(chat_key, text)`` / ``send_media(...)`` 供收件箱出站经此号发送。
    """

    def __init__(self, account: Dict[str, Any], config: Dict[str, Any]) -> None:
        self.account = account or {}
        self.config = config or {}
        self.account_id = str(self.account.get("account_id") or "")
        meta = self.account.get("meta") or {}
        self.session_name = str(meta.get("session_name") or "")
        self.session_string = str(meta.get("session_string") or "")
        self.proxy_id = str(self.account.get("proxy_id") or "")
        self.persona_ids: List[Any] = list(meta.get("persona_ids") or [])
        self.client: Any = None  # A 线 TelegramClient 实例
        self.state = "stopped"
        self.detail = ""

    def _account_cfg(self) -> Dict[str, Any]:
        """组装 A 线 TelegramClient 的 account_cfg overlay（session/代理/人设）。"""
        cfg: Dict[str, Any] = {
            "account_id": self.account_id,
            "account_label": str(self.account.get("label") or self.account_id),
            "proxy_id": self.proxy_id,
            "persona_ids": self.persona_ids,
        }
        if self.session_name:
            cfg["session_name"] = self.session_name
        if self.session_string:
            cfg["session_string"] = self.session_string
        # N4b：协议号默认把收/发镜像进统一收件箱，坐席台/收件箱可见（"有灵魂"且可托管）
        cfg["mirror_inbox"] = True
        return cfg

    async def start(self) -> None:
        ctx = get_companion_context()
        config_manager = ctx.get("config_manager")
        skill_manager = ctx.get("skill_manager")
        if config_manager is None or skill_manager is None:
            raise RuntimeError(
                "companion runtime 上下文未就绪（缺 config_manager/skill_manager）；"
                "需 app 启动时调用 set_companion_context"
            )
        if not (self.session_name or self.session_string):
            raise RuntimeError("缺少 session（需先扫码/手机登录得到 session_name 或 session_string）")

        # 重启前先清理旧 client，避免连接泄漏
        if self.client is not None:
            try:
                await self.client.stop()
            except Exception:  # noqa: BLE001
                pass
            self.client = None

        from src.client.telegram_client import TelegramClient  # 惰性：避开 pyrogram 重依赖
        self.client = TelegramClient(
            config=config_manager,
            skill_manager=skill_manager,
            ai_client=ctx.get("ai_client"),
            account_cfg=self._account_cfg(),
        )
        ok = await self.client.initialize()
        if not ok:
            self.client = None
            raise RuntimeError("A 线 TelegramClient 初始化失败（凭据/session 不可用）")
        # 编排器托管：非阻塞启动（不进入 idle()）
        await self.client.start(block=False)
        self.state = "running"
        self.detail = ""

    async def send(self, chat_key: str, text: str) -> Dict[str, Any]:
        if self.client is None:
            raise RuntimeError("A 线 client 未连接")
        target: Any = chat_key
        try:
            target = int(chat_key)
        except (TypeError, ValueError):
            target = chat_key
        # P4-4：取回真实 message.id，让编排器出站回写带 id → 已读回执（双勾）可精确绑定该行。
        # A 线 TelegramClient 暴露 send_message_return_id；缺失（旧壳）时优雅回落只回 bool。
        _fn = getattr(self.client, "send_message_return_id", None)
        if _fn is not None:
            ok, mid = await _fn(target, text)
            return {"delivered": bool(ok), "message_id": str(mid or "")}
        ok = await self.client.send_message(target, text)
        return {"delivered": bool(ok), "message_id": ""}

    async def stop(self) -> None:
        try:
            if self.client is not None:
                await self.client.stop()
        except Exception:  # noqa: BLE001
            logger.debug("[tg-companion] 停止 client 失败", exc_info=True)
        self.client = None
        self.state = "stopped"

    async def healthy(self) -> bool:
        try:
            if self.client is None or not getattr(self.client, "running", False):
                return False
            inner = getattr(self.client, "client", None)
            return bool(inner is not None and getattr(inner, "is_connected", False))
        except Exception:  # noqa: BLE001
            return False

    def status(self) -> Dict[str, Any]:
        return {
            "type": "telegram_companion",
            "session": self.session_name,
            "account_id": self.account_id,
            "state": self.state,
            "detail": self.detail,
        }


__all__ = [
    "set_companion_context",
    "get_companion_context",
    "companion_context_ready",
    "reset_companion_context",
    "companion_runtime_enabled",
    "TelegramCompanionWorker",
]
