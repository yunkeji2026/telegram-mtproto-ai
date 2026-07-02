"""W3-3M：RelationshipStager

漏斗阶段（funnel_stage）＋亲密度（intimacy_score）→ AI 对话语气指令

设计原则
────────
- **纯函数，零状态，零 I/O**：直接 import 即用，无初始化开销。
- **跨域补充**，非 conversion 专属
  ``companion_relationship.py`` 已为 conversion 域提供完整关系块；
  本模块为其余域（customer-service / conversion 域中未启用 companion 时）
  提供轻量「语气校准」指令，避免对新用户和老用户说同样的话。
- **只产出一句精炼指令**，不重复人设 / KB / 硬约束，不塞长文。
- 调用方自行决定是否注入（feature flag 见 ``stage_directive`` 返回空串的情形）。

漏斗阶段定义（与 contacts/journeys 表保持一致）
────────────────────────────────────────────────
INITIAL               → 首次接触，完全陌生
HANDOFF_SENT          → 已发手动引流消息，用户未回
LINE_ADDED            → 用户已加 LINE，尚无深度互动
LINE_ACCEPTED         → LINE 请求被接受
LINE_ENGAGED          → 已有多轮互动，关系升温
BONDED                → 长期活跃，深度关系
CONVERTED             → 已转化（付费/签单等）
LOST_HANDOFF          → 流失：引流后失联
LOST_LINE_SILENT      → 流失：加 LINE 后长时间无回
NEEDS_MANUAL_MERGE    → 系统标记需人工合并身份

亲密度分档（与 IntimacyEngine / AI Studio 看板对齐）
────────────────────────────────────────────────────
  0-25   → stranger
  25-55  → friend
  55-80  → close
  80+    → soulmate
"""

from __future__ import annotations

from typing import Optional

# ── 漏斗分组 ──────────────────────────────────────────────

_STAGE_NEW = frozenset({"INITIAL"})

_STAGE_WARM_UP = frozenset({
    "HANDOFF_SENT", "LINE_ADDED",
})

_STAGE_BUILDING = frozenset({
    "LINE_ACCEPTED", "LINE_ENGAGED",
})

# 单一来源：狭义「已成交」阶段集合 = models.WON_STAGES（P5-2c，避免与收件箱/看板口径漂移）
from src.contacts.models import WON_STAGES as _STAGE_BONDED

_STAGE_LOST = frozenset({
    "LOST_HANDOFF", "LOST_LINE_SILENT",
})

# ── 亲密度分档 ────────────────────────────────────────────

def _intim_band(score: Optional[float]) -> str:
    """0-100 分 → 'stranger' / 'friend' / 'close' / 'soulmate'"""
    if score is None:
        return "unknown"
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "unknown"
    if s >= 80:
        return "soulmate"
    if s >= 55:
        return "close"
    if s >= 25:
        return "friend"
    return "stranger"


# ── 核心公开 API ─────────────────────────────────────────

def stage_directive(
    funnel_stage: Optional[str],
    intimacy_score: Optional[float] = None,
) -> str:
    """根据漏斗阶段和亲密度生成一句语气指令，供注入 AI system prompt。

    - 返回空串 → 无需注入（调用方可直接 ``if directive: parts.append(...)``）
    - 指令格式：``【关系阶段】<一句话>``
    - 与 companion_relationship 并行，互不覆盖
    """
    fs = (funnel_stage or "INITIAL").strip().upper()
    band = _intim_band(intimacy_score)

    # ── 流失用户：优先处理，避免被其他分支覆盖 ──
    if fs in _STAGE_LOST:
        return (
            "【关系阶段】与用户曾有过联系但已沉默较长时间；"
            "自然问候即可，不要假设对方记得上次话题，也不要强行续旧。"
        )

    if fs in _STAGE_NEW:
        # 新用户：保守，不用太亲密
        return (
            "【关系阶段】用户为首次接触的新用户；"
            "保持热情但不过分熟悉，以用户节奏为准，不主动宣示亲密关系。"
        )

    if fs in _STAGE_WARM_UP:
        return (
            "【关系阶段】用户尚在观望/关系初建阶段；"
            "语气温和专业，可适当展示亲和力，但克制过度撒娇或假设亲密度。"
        )

    if fs in _STAGE_BUILDING:
        # 已有几轮互动：可以更自然
        if band in ("close", "soulmate"):
            return (
                "【关系阶段】与用户关系正快速升温，亲密度较高；"
                "可用轻松自然的语气，像老朋友聊天，但仍以用户的话题节奏为主导。"
            )
        return (
            "【关系阶段】与用户已有多轮互动，关系在建立中；"
            "可以更自然地聊天，少用客服式开场，更多跟随用户的话题延伸。"
        )

    if fs in _STAGE_BONDED:
        if band == "soulmate":
            return (
                "【关系阶段】老用户，关系深厚，亲密度极高；"
                "像老朋友甚至知心好友聊天，轻松真实，不需要任何「官方感」。"
            )
        if band == "close":
            return (
                "【关系阶段】老用户，深度互动关系；"
                "用亲切自然的语气，可以开玩笑、接梗，像熟悉的朋友。"
            )
        return (
            "【关系阶段】老用户，已有长期互动；"
            "语气比新用户更亲切自然，可以主动聊近况或承接上次的话题。"
        )

    # NEEDS_MANUAL_MERGE 或未知阶段 → 不注入（安全降级）
    return ""
