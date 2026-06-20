"""官方 API 出站 worker（Phase G 延伸：mode=official）。

把 LINE/Messenger/WhatsApp 三端**官方 API** 接入账号池编排器，使
``orch.send(platform, account_id, chat_key, text)`` 主管道（companion / protocol_autoreply）
能直接经官方通道发——而不止于各 webhook 自身的 reply 管道。

与既有 worker 的差别：官方 API 是**无状态 HTTP**（无常驻连接），故 ``start/stop`` 是 no-op，
``healthy()`` 只校验凭证齐备。发送复用 G1/G2 的官方 send 助手（已内建 Kill-Switch 守卫）。

凭证解析：优先账号 ``meta``（多官方账号），回退到平台级 config 块（单官方账号）：
- LINE       ：meta.channel_access_token | config.line.channel_access_token
- Messenger  ：meta.page_access_token    | config.facebook_messenger.page_access_token
- WhatsApp   ：meta.access_token+phone_number_id | config.whatsapp_cloud.{access_token,phone_number_id}
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 编排器接管的官方平台（platform 与 RPA/Kill-Switch 作用域命名保持一致）
OFFICIAL_PLATFORMS = ("line", "messenger", "whatsapp", "instagram", "zalo")


def _meta(account: Dict[str, Any]) -> Dict[str, Any]:
    return dict((account or {}).get("meta") or {})


def dest_from_chat_key(chat_key: str) -> str:
    """从统一收件箱 chat_key 提取官方平台「裸」收件人标识（G4b 接管发送闭环关键）。

    G4 入站镜像写入的 chat_key 形如 ``line:user:<uid>`` / ``wa:user:<num>`` /
    ``fb:user:<psid>`` / ``line:group:<gid>``；而官方 send 助手（line_push / fb_send /
    wa_send_text）要的是**裸标识**。取最后一段即可——裸标识（LINE userId「U…」、PSID、
    手机号）本身不含冒号，故对「入参已是裸标识」的调用（如 companion 主管道直传）幂等。
    """
    s = str(chat_key or "").strip()
    if not s:
        return s
    return s.rsplit(":", 1)[-1]


def _cfg_block(config: Dict[str, Any], key: str) -> Dict[str, Any]:
    return dict((config or {}).get(key) or {})


class OfficialApiWorker:
    """单账号官方 API 出站 worker（按 platform 分发到对应 send 助手）。"""

    def __init__(self, account: Dict[str, Any], config: Dict[str, Any]) -> None:
        self.account = account or {}
        self.config = config or {}
        self.platform = str(self.account.get("platform") or "").strip().lower()
        self.account_id = str(self.account.get("account_id") or "").strip() or "default"
        self.state = "stopped"
        self.detail = ""

    # ── 凭证 ─────────────────────────────────────────────────────────────────
    def _creds(self) -> Dict[str, str]:
        m = _meta(self.account)
        if self.platform == "line":
            block = _cfg_block(self.config, "line")
            return {"access_token": str(m.get("channel_access_token")
                                        or block.get("channel_access_token") or "")}
        if self.platform == "messenger":
            block = _cfg_block(self.config, "facebook_messenger")
            return {"access_token": str(m.get("page_access_token")
                                        or block.get("page_access_token") or "")}
        if self.platform == "whatsapp":
            block = _cfg_block(self.config, "whatsapp_cloud")
            return {
                "access_token": str(m.get("access_token") or block.get("access_token") or ""),
                "phone_number_id": str(m.get("phone_number_id")
                                       or block.get("phone_number_id") or ""),
            }
        if self.platform == "instagram":
            block = _cfg_block(self.config, "instagram")
            return {
                "access_token": str(m.get("page_access_token")
                                    or block.get("page_access_token") or ""),
                "ig_id": str(m.get("ig_id") or block.get("ig_id") or ""),
            }
        if self.platform == "zalo":
            block = _cfg_block(self.config, "zalo")
            return {
                "access_token": str(m.get("access_token") or block.get("access_token") or ""),
                "message_type": str(m.get("message_type")
                                    or block.get("message_type") or "cs"),
            }
        return {}

    def _creds_ok(self) -> bool:
        c = self._creds()
        if self.platform == "whatsapp":
            return bool(c.get("access_token") and c.get("phone_number_id"))
        return bool(c.get("access_token"))

    # ── Worker 接口 ──────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self.platform not in OFFICIAL_PLATFORMS:
            raise RuntimeError(f"不支持的官方平台: {self.platform}")
        if not self._creds_ok():
            raise RuntimeError(f"官方 {self.platform} 缺少凭证")
        self.state = "running"
        self.detail = ""

    async def stop(self) -> None:
        self.state = "stopped"

    async def healthy(self) -> bool:
        return self.state == "running" and self._creds_ok()

    def status(self) -> Dict[str, Any]:
        return {"type": f"{self.platform}_official", "account_id": self.account_id,
                "state": self.state, "detail": self.detail}

    async def send(self, chat_key: str, text: str) -> Dict[str, Any]:
        """chat_key = 收件人标识（接受收件箱前缀形式 ``<plat>:user:<id>`` 或裸标识，自动归一）。"""
        c = self._creds()
        dest = dest_from_chat_key(chat_key)
        if self.platform == "line":
            from src.integrations.line_webhook import line_push
            ok = await line_push(dest, text, c["access_token"],
                                 account_id=self.account_id)
            return {"delivered": bool(ok)}
        if self.platform == "messenger":
            from src.integrations.facebook_webhook import fb_send_with_window_fallback
            out = await fb_send_with_window_fallback(
                dest, text, c["access_token"], account_id=self.account_id)
            return {"delivered": bool(out.get("ok")),
                    "message_id": str(((out.get("data") or {}).get("message_id")) or "")}
        if self.platform == "whatsapp":
            from src.integrations.whatsapp_cloud import wa_send_text
            out = await wa_send_text(dest, text, c["phone_number_id"], c["access_token"])
            data = out.get("data") or {}
            mid = ""
            try:
                mid = str(((data.get("messages") or [{}])[0]).get("id") or "")
            except Exception:
                mid = ""
            return {"delivered": bool(out.get("ok")), "message_id": mid}
        if self.platform == "instagram":
            from src.integrations.instagram_webhook import ig_send_text
            out = await ig_send_text(dest, text, c.get("ig_id", ""),
                                     c["access_token"], account_id=self.account_id)
            return {"delivered": bool(out.get("ok")),
                    "message_id": str(((out.get("data") or {}).get("message_id")) or "")}
        if self.platform == "zalo":
            from src.integrations.zalo_webhook import zalo_send_text
            out = await zalo_send_text(dest, text, c["access_token"],
                                       message_type=c.get("message_type", "cs"),
                                       account_id=self.account_id)
            data = out.get("data") or {}
            mid = str((data.get("data") or {}).get("message_id") or "") if isinstance(data, dict) else ""
            return {"delivered": bool(out.get("ok")), "message_id": mid}
        raise RuntimeError(f"不支持的官方平台: {self.platform}")


def official_pipeline_enabled(config: Dict[str, Any]) -> bool:
    """官方入站是否走 protocol_autoreply 主管道（G4c）。

    默认 **False** → 各 webhook 维持自身 SkillManager 自答（零回归）。
    开启后官方入站 → `maybe_auto_reply`：享 kill-switch 决策期早退 / canary / 陪伴记忆 /
    限速熔断 / 审计 / 转人工，且回复经 `orch.send`→官方 worker 出站（需官方账号 mode=official
    且被编排器接管；否则 run_autoreply 因 disabled/无 worker 不发——属预期，见 DEVLOG G4c）。
    """
    return bool(((config or {}).get("official_pipeline") or {}).get("enabled"))


def official_enabled(config: Dict[str, Any], platform: str) -> bool:
    """该官方平台是否在 config 中启用（platform_login.official.<platform> 或对应通道块 enabled）。"""
    p = str(platform or "").lower()
    pl = ((config or {}).get("platform_login") or {}).get("official") or {}
    if pl.get(p, {}).get("enabled"):
        return True
    # 回退：对应官方通道块自身 enabled 也视为开
    key = {"line": "line", "messenger": "facebook_messenger",
           "whatsapp": "whatsapp_cloud", "instagram": "instagram",
           "zalo": "zalo"}.get(p)
    if key and ((config or {}).get(key) or {}).get("enabled"):
        return True
    return False


def register_official_workers(config: Dict[str, Any]) -> None:
    """按需把官方 worker 注册进编排器（幂等、门控）。供 ensure_builtin_workers 调。"""
    from src.integrations.account_orchestrator import (
        get_worker_factory, register_worker,
    )
    for platform in OFFICIAL_PLATFORMS:
        try:
            if official_enabled(config, platform) and get_worker_factory(platform, "official") is None:
                register_worker(platform, "official",
                                lambda acc, cfg: OfficialApiWorker(acc, cfg))
                logger.info("[orchestrator] 官方 worker 已注册: %s:official", platform)
        except Exception:
            logger.debug("[orchestrator] 注册 %s official worker 失败", platform, exc_info=True)


__all__ = [
    "OfficialApiWorker", "official_enabled", "official_pipeline_enabled",
    "register_official_workers", "dest_from_chat_key", "OFFICIAL_PLATFORMS",
]
