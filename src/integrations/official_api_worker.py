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
            return self._result(
                out, str(((out.get("data") or {}).get("message_id")) or ""))
        if self.platform == "whatsapp":
            from src.integrations.whatsapp_cloud import wa_send_text
            out = await wa_send_text(dest, text, c["phone_number_id"], c["access_token"])
            data = out.get("data") or {}
            mid = ""
            try:
                mid = str(((data.get("messages") or [{}])[0]).get("id") or "")
            except Exception:
                mid = ""
            return self._result(out, mid)
        if self.platform == "instagram":
            from src.integrations.instagram_webhook import (
                ig_send_text, ig_send_with_window_fallback,
            )
            ig_cfg = _cfg_block(self.config, "instagram")
            if bool(_meta(self.account).get("human_agent_fallback")
                    or ig_cfg.get("human_agent_fallback")):
                out = await ig_send_with_window_fallback(
                    dest, text, c.get("ig_id", ""), c["access_token"],
                    account_id=self.account_id)
            else:
                out = await ig_send_text(dest, text, c.get("ig_id", ""),
                                         c["access_token"], account_id=self.account_id)
            return self._result(
                out, str(((out.get("data") or {}).get("message_id")) or ""))
        if self.platform == "zalo":
            from src.integrations.zalo_webhook import zalo_send_text
            out = await zalo_send_text(dest, text, c["access_token"],
                                       message_type=c.get("message_type", "cs"),
                                       account_id=self.account_id)
            data = out.get("data") or {}
            mid = str((data.get("data") or {}).get("message_id") or "") if isinstance(data, dict) else ""
            return self._result(out, mid)
        raise RuntimeError(f"不支持的官方平台: {self.platform}")

    def _public_media_url(self, media_url: str) -> str:
        """把 ``/static`` 相对 URL 拼成公网绝对 https URL（LINE/Messenger 必须，FB/LINE 自取字节）。

        - 已是 http(s) 绝对 URL → 原样返回。
        - 否则取 ``config.official_media.public_base_url`` 前缀拼接；未配置 → 返回空串
          （调用方据此回 ``no_public_url``，可观测而非静默失败）。
        """
        u = str(media_url or "").strip()
        if u.lower().startswith(("http://", "https://")):
            return u
        base = str(
            (((self.config or {}).get("official_media") or {}).get("public_base_url")) or ""
        ).strip().rstrip("/")
        if not base or not u:
            return ""
        if not u.startswith("/"):
            u = "/" + u
        return base + u

    async def send_media(
        self, chat_key: str, *, media_path: str, media_type: str,
        caption: str = "", media_url: str = "",
    ) -> Dict[str, Any]:
        """官方通道媒体出站（语音/图片/…），与 ``orch.send_media`` 契约一致。

        - WhatsApp：上传本地文件 → media_id 发送（**无需公网 URL**）。
        - LINE / Messenger：按**公网 https URL** 发送（需配 ``official_media.public_base_url``，
          否则回 ``error_kind=no_public_url``）。
        - Instagram / Zalo：官方 API 媒体出站暂未接入 → ``not_supported``。
        """
        c = self._creds()
        dest = dest_from_chat_key(chat_key)
        mt = str(media_type or "").lower()
        if self.platform == "whatsapp":
            from src.integrations.whatsapp_cloud import wa_send_media
            out = await wa_send_media(
                dest, media_path, c["phone_number_id"], c["access_token"],
                media_type=mt, caption=caption)
            mid = ""
            try:
                mid = str((((out.get("data") or {}).get("messages") or [{}])[0]).get("id") or "")
            except Exception:
                mid = ""
            return self._result(out, mid)
        if self.platform == "line":
            pub = self._public_media_url(media_url)
            if not pub:
                return {"delivered": False, "error_kind": "no_public_url",
                        "error": "LINE 媒体需公网 https URL（配 official_media.public_base_url）"}
            dur = 0
            if mt in ("voice", "audio"):
                try:
                    from src.client.voice_sender import probe_audio_duration_ms
                    dur = int(probe_audio_duration_ms(media_path) or 0)
                except Exception:
                    dur = 0
            from src.integrations.line_webhook import line_push_media
            ok = await line_push_media(
                dest, pub, c["access_token"], media_type=mt,
                duration_ms=dur, account_id=self.account_id)
            return {"delivered": True} if ok else {
                "delivered": False, "error_kind": "send_failed"}
        if self.platform == "messenger":
            # 优先公网 URL；未配则 multipart 字节上传（免公网托管，与 WhatsApp 对齐）。
            pub = self._public_media_url(media_url)
            if pub:
                from src.integrations.facebook_webhook import fb_send_attachment
                out = await fb_send_attachment(
                    dest, pub, c["access_token"], media_type=mt, account_id=self.account_id)
            else:
                from src.integrations.facebook_webhook import fb_send_attachment_upload
                out = await fb_send_attachment_upload(
                    dest, media_path, c["access_token"], media_type=mt,
                    account_id=self.account_id)
            return self._result(
                out, str(((out.get("data") or {}).get("message_id")) or ""))
        if self.platform == "instagram":
            # IG DM 附件仅支持公网 URL（不支持 filedata 上传）。
            pub = self._public_media_url(media_url)
            if not pub:
                return {"delivered": False, "error_kind": "no_public_url",
                        "error": "Instagram 媒体需公网 https URL（配 official_media.public_base_url）"}
            from src.integrations.instagram_webhook import ig_send_attachment
            out = await ig_send_attachment(
                dest, pub, c.get("ig_id", ""), c["access_token"],
                media_type=mt, account_id=self.account_id)
            return self._result(
                out, str(((out.get("data") or {}).get("message_id")) or ""))
        if self.platform == "zalo":
            # Zalo OA API 无语音消息出站能力（图片/文件另需 upload 流程，暂未接入）。
            return {"delivered": False, "error_kind": "not_supported",
                    "error": "Zalo OA API 暂不支持语音/媒体消息出站"}
        return {"delivered": False, "error_kind": "not_supported",
                "error": f"{self.platform} 官方通道媒体出站暂未接入"}

    @staticmethod
    def _result(out: Dict[str, Any], message_id: str) -> Dict[str, Any]:
        """统一出站结果：delivered + message_id，失败时透出 error_kind（窗口/token/限速…）。

        让上层（pipeline/可观测）能据 ``error_kind`` 分流，而非把失败默默当"没发出"。
        """
        res: Dict[str, Any] = {"delivered": bool(out.get("ok")), "message_id": message_id}
        if not out.get("ok"):
            res["error_kind"] = str(out.get("error_kind") or "unknown")
            res["error"] = str(out.get("error") or "")
        return res


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
