"""官方通道入站统一处理骨架（Phase H：抽出 G4 镜像 + G4c 管道/自答分支）。

LINE/Messenger/WhatsApp（G 阶段）各自内联了同一套「入站镜像进收件箱 → 走主管道 or 自答」
逻辑。新平台 Instagram/Zalo 接入时复用本骨架，避免重复代码与**漏接护栏**（kill-switch/
canary/记忆/审计/转人工只在主管道里，自答路径不享）。

一条入站文本的标准流转：
1. 镜像 `direction=in` 进收件箱（坐席台可见/SLA/危机）——always（best-effort）。
2. `use_pipeline=True` → `maybe_auto_reply(payload)`：交 protocol_autoreply 主管道决策+发送，
   回复经 `orch.send`→官方 worker 出站（出站镜像由 orch.send 负责）。**返回 True 表示已托管，
   调用方应跳过自答**。
3. `use_pipeline=False` → 返回 False，调用方走各自既有 SkillManager 自答路径（零回归）。

全程 best-effort：任何异常吞掉，绝不影响官方 webhook 收发主流程。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def inbox_will_autosend(platform: str, account_id: str, chat_key: str) -> bool:
    """统一收件箱 autosend（System Z）是否会接管该会话的**全自动**回复。

    返回 True 仅当**两者皆满足**：
      1. 会话 ``automation_mode == 'auto_ai'``（坐席台「🚀 全自动」开关）；
      2. 编排器拥有该账号（``orch.send`` 投得出，否则 System Z 发不出 → 不该让位）。

    官方 webhook 据此早退、把全自动统一交给 System Z（与 Telegram 同一条：人设 + 语言
    跟随 + 风控分级 + 拟人延迟），避免与 webhook 自答**双发**；任一不满足 → 返回 False
    （webhook 维持原有 SkillManager 自答，零回归）。高风险消息 System Z 不放行 autosend
    （停泊待人工），让位即正确地不抢发——风控优先。
    """
    plat = str(platform or "").lower()
    acct = str(account_id or "default")
    try:
        from src.integrations.protocol_bridge import get_inbox_store
        store = get_inbox_store()
        if store is None:
            return False
        from src.inbox.normalizer import conv_id
        cid = conv_id(plat, acct, str(chat_key))
        if str(store.get_automation_mode(cid) or "").lower() != "auto_ai":
            return False
    except Exception:
        return False
    try:
        from src.integrations.account_orchestrator import get_orchestrator
        return bool(get_orchestrator().owns(plat, acct))
    except Exception:
        return False


async def process_official_inbound(
    *,
    platform: str,
    account_id: str,
    chat_key: str,
    text: str,
    name: str = "",
    msg_id: str = "",
    use_pipeline: bool = False,
) -> bool:
    """镜像入站并（按开关）委托主管道。返回 True = 已托管（管道或 System Z），调用方跳过自答。"""
    try:
        from src.integrations.shared.inbox_mirror import mirror_to_inbox
        mirror_to_inbox(platform, account_id, chat_key, text,
                        direction="in", name=name, msg_id=msg_id)
    except Exception:
        logger.debug("[official_inbound] 入站镜像失败 platform=%s", platform, exc_info=True)

    # auto_ai 让位：该会话由统一收件箱 autosend(System Z) 全自动接管 → 跳过自答/管道，避免双发。
    if inbox_will_autosend(platform, account_id, chat_key):
        return True

    if not use_pipeline:
        return False

    try:
        from src.integrations.protocol_bridge import make_message, maybe_auto_reply
        await maybe_auto_reply(make_message(
            platform=platform, account_id=account_id, chat_key=chat_key,
            text=text, direction="in", name=name, msg_id=msg_id))
    except Exception:
        logger.debug("[official_inbound] 主管道回复失败 platform=%s", platform, exc_info=True)
    return True


# 入站媒体类型 → 收件箱占位文案（坐席台一眼看出「客户发了什么」，Phase I1）。
_MEDIA_PLACEHOLDER = {
    "image": "[图片]", "photo": "[图片]", "sticker": "[贴纸]",
    "audio": "[语音]", "voice": "[语音]", "video": "[视频]",
    "file": "[文件]", "document": "[文件]", "location": "[位置]",
    "contact": "[名片]", "gif": "[GIF]",
}


def media_placeholder(media_type: str) -> str:
    """非文字消息的收件箱占位文案；未知类型回退 ``[媒体]``。"""
    return _MEDIA_PLACEHOLDER.get(str(media_type or "").strip().lower(), "[媒体]")


def mirror_inbound_media(
    *, platform: str, account_id: str, chat_key: str, media_type: str,
    name: str = "", msg_id: str = "", media_ref: str = "",
) -> bool:
    """把一条入站**非文字**消息以占位形式镜像进收件箱（让坐席看到并可接管）。

    best-effort：失败吞掉，绝不影响官方 webhook 主流程。返回是否投递到 sink。
    """
    try:
        from src.integrations.shared.inbox_mirror import mirror_to_inbox
        mt = str(media_type or "").strip().lower()
        return mirror_to_inbox(
            platform, account_id, chat_key, media_placeholder(mt),
            direction="in", name=name, msg_id=msg_id,
            media_type=mt, media_ref=media_ref)
    except Exception:
        logger.debug("[official_inbound] 入站媒体镜像失败 platform=%s", platform, exc_info=True)
        return False


async def mirror_official_outbound(
    *, platform: str, account_id: str, chat_key: str, text: str,
) -> None:
    """自答路径发出后镜像 `direction=out`（pipeline 模式不用——orch.send 已回写）。"""
    try:
        from src.integrations.shared.inbox_mirror import mirror_to_inbox
        mirror_to_inbox(platform, account_id, chat_key, text, direction="out")
    except Exception:
        logger.debug("[official_inbound] 出站镜像失败 platform=%s", platform, exc_info=True)


__all__ = [
    "process_official_inbound", "mirror_official_outbound",
    "mirror_inbound_media", "media_placeholder", "inbox_will_autosend",
]
