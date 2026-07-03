"""LINE 协议模式（web mode 等价）扫码登录 provider（M7）。

LINE 官方**没有可嵌入的完整网页聊天端**，但社区有等价 WhatsApp Baileys 的逆向协议库——
通过 **LINE Chrome 扩展网关**（``line-chrome-gw.line-apps.com``）直连，支持二维码登录 +
收发。本模块用 ``okline``（纯 Python，进程内跑，无需 Node 微服务）实现 LINE 的
「协议多开」登录，功能对齐官方：扫码登录、真实消息 id、通讯录/群、真实昵称头像。

架构（仿 Telegram pyrogram 的进程内 worker，而非 Baileys 的 Node 微服务）：
- 登录：``okline`` 的二维码流是回调驱动的阻塞长轮询（create_session→qr→长轮询扫码→PIN→
  长轮询确认→login_v2）。这里放到**后台线程**里驱动，把 QR/PIN/状态写进共享状态，
  provider 的 ``poll`` 只读状态——契合统一收件箱的「发起→轮询」模型。
- 成功：落 tokens 到 ``sessions/line/<mid>.json``、写账号注册表、富集自身昵称/头像。
- 收发：见 ``account_orchestrator.LineProtocolWorker``（okline ``Bot`` 后台线程收消息 →
  protocol_bridge 落库 + 自动回复；``send_text`` 出站）。

落地约束（与 M2/M3 一致的谨慎姿态）：
- ``okline`` 为**非官方逆向库**，违反 LINE ToS、有封号风险、LINE 改网关/WASM 时可能失效；
  部分 E2EE(Letter Sealing) 私聊消息可能读不全。故默认**不启用**，需
  ``config.platform_login.line.protocol_enabled: true`` 且已 ``pip install okline`` 显式开启。
- 缺库 / 未开闸 → 不注册（前端「网页」方式灰显「未启用」），主进程零行为变化。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, Optional

from src.integrations.account_registry import get_account_registry
from src.integrations.platform_login import register_login_provider

logger = logging.getLogger(__name__)

_DEFAULT_SESSIONS_DIR = os.path.join("sessions", "line")
_registered = False


def is_okline_available() -> bool:
    try:
        import okline  # noqa: F401
        return True
    except Exception:
        return False


def protocol_enabled(config: Dict[str, Any]) -> bool:
    pl = (config or {}).get("platform_login", {}) or {}
    ln = pl.get("line", {}) or {}
    return bool(ln.get("protocol_enabled", False))


def sessions_dir(config: Dict[str, Any]) -> str:
    pl = (config or {}).get("platform_login", {}) or {}
    ln = pl.get("line", {}) or {}
    return str(ln.get("sessions_dir") or _DEFAULT_SESSIONS_DIR)


def tokens_path(config: Dict[str, Any], mid: str) -> str:
    return os.path.join(sessions_dir(config), f"{mid}.json")


def _qr_data_uri(qr_url: str) -> str:
    """把 QR 回调 URL 渲染成二维码图片 data URI（前端弹窗直接显示）。"""
    if not qr_url:
        return ""
    try:
        import base64
        import io
        import qrcode
        img = qrcode.make(qr_url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        logger.debug("[line_protocol] 二维码渲染失败", exc_info=True)
        return ""


def line_picture_url(picture_path: str) -> str:
    """把 LINE ``picturePath`` 拼成可直接渲染的 obs CDN 头像 URL（已是 http 则原样；空→空）。

    纯函数（OBS_BASE 取不到时回落硬编码域名），供 self_profile 与 peer 头像解析共用、便于单测。
    LINE obs 直链是内容寻址的稳定 URL（无 scontent 那种时效 token）→ 可直接落库 ``avatar_url``
    由前端 priority ① 渲染，无需下载落 /static 代理（与 messenger 的会过期直链不同）。
    """
    pic = str(picture_path or "").strip()
    if not pic:
        return ""
    if pic.startswith("http"):
        return pic
    try:
        from okline import OBS_BASE
        return str(OBS_BASE).rstrip("/") + "/" + pic.lstrip("/")
    except Exception:
        return "https://obs.line-scdn.net/" + pic.lstrip("/")


def _self_profile_fields(client: Any) -> tuple[str, str]:
    """best-effort 读取账号自身昵称/头像 URL（供 self_profile 富集）。"""
    try:
        prof = client.get_profile()
        d = prof if isinstance(prof, dict) else getattr(prof, "raw", {}) or {}
        name = str(d.get("displayName") or getattr(prof, "display_name", "") or "")
        pic = str(d.get("picturePath") or getattr(prof, "picture_path", "") or "")
        return name, line_picture_url(pic)
    except Exception:
        logger.debug("[line_protocol] get_profile 失败", exc_info=True)
        return "", ""


def _drive_qr_login(client: Any, state: Dict[str, Any], config: Dict[str, Any]) -> None:
    """在后台线程里驱动 okline 的二维码登录流，把 QR/PIN/状态写进 ``state``（同步、阻塞）。

    单测可直接调用本函数（传 fake client），无需起线程。
    """
    def on_qr(url: str) -> None:
        state["qr_url"] = url
        state["qr_image"] = _qr_data_uri(url)
        state["status"] = "pending"

    def on_pin(pin: str) -> None:
        state["pin"] = str(pin or "")
        state["status"] = "scanned"
        state["detail"] = f"在手机 LINE 上输入 PIN：{pin}"

    try:
        result = client.qr_login(on_qr=on_qr, on_pin=on_pin, wait_seconds=170.0)
    except Exception as ex:  # noqa: BLE001
        logger.debug("[line_protocol] qr_login 失败", exc_info=True)
        state["status"] = "failed"
        state["detail"] = str(ex)
        return

    mid = str(getattr(result, "mid", "") or "")
    if not getattr(result, "success", False) or not mid:
        state["status"] = "failed"
        state["detail"] = str(getattr(result, "display_message", "") or "login failed")
        return

    state["mid"] = mid
    try:
        os.makedirs(sessions_dir(config), exist_ok=True)
        client.save_tokens(tokens_path(config, mid))
    except Exception:  # noqa: BLE001
        logger.debug("[line_protocol] save_tokens 失败", exc_info=True)
    name, avatar = _self_profile_fields(client)
    state["name"] = name
    state["avatar_url"] = avatar
    state["status"] = "authorized"


def make_provider(config: Dict[str, Any]):

    async def _provider(request: Any, platform: str, mode: str, account_id: str,
                        ctx: Optional[Dict[str, Any]] = None):
        if not is_okline_available():
            return {"instruction": "未安装 LINE 协议库 okline。请先 `pip install okline` 再重试。"}
        try:
            from okline import OkLine
            client = OkLine()
        except Exception as ex:  # noqa: BLE001
            logger.debug("[line_protocol] 初始化 OkLine 失败", exc_info=True)
            return {"instruction": f"初始化 LINE 协议客户端失败（{ex}）。"}

        state: Dict[str, Any] = {
            "status": "pending", "qr_url": "", "qr_image": "", "pin": "",
            "mid": "", "name": "", "avatar_url": "", "detail": "", "_persisted": False,
        }
        t = threading.Thread(
            target=_drive_qr_login, args=(client, state, config), daemon=True)
        t.start()
        # 等首个二维码（最多 ~8s）
        deadline = time.time() + 8.0
        while not state["qr_image"] and state["status"] == "pending" and time.time() < deadline:
            time.sleep(0.2)

        async def _poll(session: Any) -> Dict[str, Any]:
            st = str(state.get("status") or "pending")
            mid = str(state.get("mid") or "")
            if st == "authorized" and mid and not state.get("_persisted"):
                state["_persisted"] = True
                try:
                    get_account_registry().upsert(
                        "line", mid, mode="protocol", status="online",
                        meta={"tokens_path": tokens_path(config, mid)})
                except Exception:  # noqa: BLE001
                    logger.debug("[line_protocol] 注册表写入失败", exc_info=True)
                try:
                    from src.integrations.account_self_profile import enrich_from_fields
                    await enrich_from_fields(
                        "line", mid, name=str(state.get("name") or ""),
                        avatar_url=str(state.get("avatar_url") or ""), config=config)
                except Exception:  # noqa: BLE001
                    logger.debug("[line_protocol] self_profile 富集失败（忽略）", exc_info=True)
            return {"status": st, "account_id": mid,
                    "detail": str(state.get("detail") or ""),
                    "qr_image": ("" if st == "authorized" else str(state.get("qr_image") or ""))}

        async def _cancel(session: Any) -> None:
            # daemon 线程无法强杀；置 failed 让其 qr_login 超时后自然结束。
            state["status"] = "failed"
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

        return {
            "qr_image": str(state.get("qr_image") or ""),
            "instruction": "用手机 LINE 扫码：设置 → 我的账户 →「用其他设备登录 / 登录中的设备」出示二维码，"
                           "用主设备 LINE 扫描；如提示 PIN，请在手机上输入弹出的数字。",
            "poll": _poll,
            "cancel": _cancel,
            "state": state,
        }

    return _provider


def maybe_register(config: Dict[str, Any]) -> bool:
    """按需注册 LINE protocol provider（幂等）。

    仅当 ``protocol_enabled: true`` 且已安装 okline 时注册。
    """
    global _registered
    if _registered:
        return True
    if not protocol_enabled(config):
        return False
    if not is_okline_available():
        logger.warning("[line_protocol] protocol_enabled=true 但未安装 okline，跳过注册")
        return False
    register_login_provider("line", "protocol", make_provider(config))
    _registered = True
    logger.info("[line_protocol] LINE protocol 登录 provider 已注册")
    return True
