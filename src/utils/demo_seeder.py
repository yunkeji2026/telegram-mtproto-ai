"""C1-2 试用/Demo：一键铺示例数据，让看板当场有「漂亮的数字」。

设计要点
========
- **命名空间隔离**：所有示例会话 conversation_id 以 ``demo:`` 开头，可一键整体清空，
  **绝不污染真实数据**（清空只按前缀删，见 ``InboxStore.purge_demo``）。
- **复用既有写接口**：仅用 ``upsert_conversation`` / ``ingest_message`` /
  ``record_draft_audit`` 公共方法，不碰内部 SQL。
- **覆盖三看板**：造入站/出站消息（用量+ROI 消息量）、多种草稿处置（autosend/
  edit_send/approved/rejected/blocked → 质量+ROI+用量）、多坐席（活跃坐席数）。
- **幂等可重复**：再次 seed 前可先 purge；消息 PK 去重，重复 seed 不会翻倍。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict

logger = logging.getLogger(__name__)

DEMO_PREFIX = "demo:"

# 示例客户（platform, 名字, 语言）
_CUSTOMERS = [
    ("telegram", "Alice Wang", "zh"),
    ("telegram", "John Carter", "en"),
    ("line", "佐藤花子", "ja"),
    ("line", "陈伟", "zh"),
    ("messenger", "Maria Silva", "pt"),
    ("web", "访客 8821", "zh"),
]

# 示例问答（客户问 → AI/坐席答）
_QA = [
    ("你们支持货到付款吗？", "支持的，下单时选择货到付款即可～"),
    ("How long does shipping take?", "Usually 3-5 business days. We'll send a tracking link once shipped."),
    ("送料はいくらですか？", "5000円以上のご注文で送料無料です。"),
    ("这个有现货吗？", "有现货的，现在下单今天就能发出哦。"),
    ("Can I get a discount?", "We have a 10% off code for first orders: WELCOME10 🎉"),
    ("怎么退货？", "7 天无理由退货，联系客服开退货单即可。"),
]

# 处置剧本：(autopilot_level, action, 是否有坐席, agent)
_DISPOSITIONS = [
    ("L2", "autosend", False, ""),
    ("L2", "autosend", False, ""),
    ("L3", "approved", True, "demo_agent_a"),
    ("L3", "edit_send", True, "demo_agent_a"),
    ("L3", "edit_send", True, "demo_agent_b"),
    ("L4", "blocked", False, ""),
    ("L3", "rejected", True, "demo_agent_b"),
    ("L2", "autosend", False, ""),
]


def demo_status(inbox) -> Dict[str, Any]:
    """当前 demo 数据现状（present + 计数）。"""
    if inbox is None or not hasattr(inbox, "count_demo"):
        return {"available": False, "present": False, "counts": {}}
    counts = inbox.count_demo(DEMO_PREFIX)
    present = any(int(v or 0) > 0 for v in counts.values())
    return {"available": True, "present": present, "counts": counts}


def seed_demo(inbox, *, days: int = 14, kb_store=None, config_manager=None) -> Dict[str, Any]:
    """铺示例数据：会话 + 消息 + 草稿处置（跨 days 天分布）。返回汇总计数。

    幂等：先清空既有 demo 数据再铺，避免重复累积。
    """
    from src.inbox.models import InboxConversation, InboxMessage

    if inbox is None:
        return {"ok": False, "detail": "inbox_store 不可用"}

    # 幂等：先清旧 demo
    if hasattr(inbox, "purge_demo"):
        inbox.purge_demo(DEMO_PREFIX)

    now = time.time()
    n_conv = n_msg = n_audit = 0
    span = max(1, int(days or 14))

    for ci, (platform, name, lang) in enumerate(_CUSTOMERS):
        cid = f"{DEMO_PREFIX}{platform}:cust{ci}"
        # 每个客户分布在最近 span 天里的若干轮问答
        rounds = 2 + (ci % 3)
        last_ts = now
        last_text = ""
        for r in range(rounds):
            q, a = _QA[(ci + r) % len(_QA)]
            # 时间在 [now - span 天, now] 内散布
            base = now - (span - 1) * 86400 * ((ci + r) % span) / max(1, span)
            t_in = base + r * 600
            t_out = t_in + 90
            inbox.ingest_message(InboxMessage(
                conversation_id=cid, platform_msg_id=f"d{ci}_{r}_in",
                direction="in", text=q, source_lang=lang, ts=t_in))
            n_msg += 1
            inbox.ingest_message(InboxMessage(
                conversation_id=cid, platform_msg_id=f"d{ci}_{r}_out",
                direction="out", text=a, ts=t_out))
            n_msg += 1
            # 草稿处置（驱动质量/ROI/用量）
            lvl, action, _has_agent, agent = _DISPOSITIONS[
                (ci * rounds + r) % len(_DISPOSITIONS)]
            inbox.record_draft_audit(
                f"demo_draft_{ci}_{r}", autopilot_level=lvl, action=action,
                agent_id=agent, conversation_id=cid, ts=t_out)
            n_audit += 1
            last_ts, last_text = t_out, a
        inbox.upsert_conversation(InboxConversation(
            conversation_id=cid, platform=platform, account_id="demo",
            chat_key=f"cust{ci}", display_name=name, language=lang,
            last_text=last_text, last_ts=last_ts, unread=0,
            contact_id=f"{DEMO_PREFIX}contact{ci}"))
        n_conv += 1

    # 知识库冷启动包（可选；让 KB 看板/回复也有内容）
    kb_seeded = 0
    if kb_store is not None:
        try:
            from src.utils.kb_starter import seed_starter_pack
            added, _skipped, _titles = seed_starter_pack(kb_store, "general")
            kb_seeded = int(added or 0)
        except Exception:
            logger.debug("demo KB 冷启动失败（已忽略）", exc_info=True)

    return {"ok": True, "conversations": n_conv, "messages": n_msg,
            "draft_audits": n_audit, "kb_seeded": kb_seeded}


def clear_demo(inbox) -> Dict[str, Any]:
    """一键清空 demo 数据（按命名空间前缀），返回删除计数。"""
    if inbox is None or not hasattr(inbox, "purge_demo"):
        return {"ok": False, "detail": "inbox_store 不可用"}
    removed = inbox.purge_demo(DEMO_PREFIX)
    return {"ok": True, "removed": removed}
