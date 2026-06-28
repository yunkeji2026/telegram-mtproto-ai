"""官方通道入站/出站镜像进统一收件箱（Phase G4）。

LINE/Messenger/WhatsApp Cloud 的官方 webhook 此前**直接喂 SkillManager 自答**，绕过统一
收件箱——坐席台看不到、接管不了官方渠道对话。本助手把官方渠道的「收/发」**旁路镜像**进
收件箱 sink（与 A 线 `sender._emit_inbox`、协议 worker `emit_incoming` 同一机制），让官方
渠道成为统一收件箱一等公民：可见、可监控 SLA、危机可被坐席接管。

设计：
- **纯旁路、best-effort**：sink 未注册（inbox 关）→ `emit_incoming` 静默丢弃；任何异常吞掉，
  绝不影响官方 webhook 既有的收发主流程（零回归）。
- **不触发 maybe_auto_reply**：回复仍由各 webhook 既有 SkillManager 链产出，避免双重回复。
  （把官方入站完整迁到 protocol_autoreply 管道是后续 G4b，见 DEVLOG。）
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def mirror_to_inbox(
    platform: str,
    account_id: str,
    chat_key: str,
    text: str,
    *,
    direction: str = "in",
    name: str = "",
    msg_id: str = "",
    media_type: str = "",
    media_ref: str = "",
    chat_type: str = "",
) -> bool:
    """把一条官方渠道消息镜像进统一收件箱。返回是否成功投递到 sink。

    ``media_type``/``media_ref``：入站媒体（图片/语音/视频…）可见化用——坐席台据此显示
    「[图片]」等占位并知道有非文字内容到达（Phase I1）。
    ``chat_type``：上游会话类型（如 LINE 的 ``group``/``room``/``user``）。带上则群组/房间
    会被正确分流到「群组动态」，不再误刷 SLA「严重超时/待接管」。
    """
    if not chat_key:
        return False
    try:
        from src.integrations.protocol_bridge import emit_incoming, make_message
        source = {"chat_type": str(chat_type).strip().lower()} if chat_type else None
        emit_incoming(make_message(
            platform=str(platform or ""),
            account_id=str(account_id or "default"),
            chat_key=str(chat_key),
            text=str(text or ""),
            direction=str(direction or "in"),
            name=str(name or ""),
            msg_id=str(msg_id or ""),
            media_type=str(media_type or ""),
            media_ref=str(media_ref or ""),
            source=source,
        ))
        return True
    except Exception:
        logger.debug("[inbox_mirror] 镜像失败 platform=%s", platform, exc_info=True)
        return False


__all__ = ["mirror_to_inbox"]
