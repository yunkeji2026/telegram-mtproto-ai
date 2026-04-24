"""
陪伴关系阶段（conversion 域）：持久化于 user_context.companion_relationship[chat_key]，
供 AI 提示注入与回复后演进。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

# 阶段顺序由低到高
STAGE_ORDER: Tuple[str, ...] = ("initial", "warming", "intimate", "steady")

STAGE_LABEL_ZH = {
    "initial": "初识",
    "warming": "试探/升温",
    "intimate": "暧昧陪伴",
    "steady": "稳定陪伴",
}


def chat_storage_key(chat_id: Any) -> str:
    s = str(chat_id).strip() if chat_id is not None else ""
    return s if s else "_default"


def get_rel_state(user_ctx: Dict[str, Any], chat_id: Any) -> Dict[str, Any]:
    """返回当前会话的关系状态 dict（可原地修改）。"""
    key = chat_storage_key(chat_id)
    root = user_ctx.setdefault("companion_relationship", {})
    if not isinstance(root, dict):
        root = {}
        user_ctx["companion_relationship"] = root
    st = root.get(key)
    if not isinstance(st, dict):
        st = {}
    st.setdefault("stage", "initial")
    st.setdefault("exchange_count", 0)
    st.setdefault("updated_at", 0.0)
    root[key] = st
    return st


def _thresholds(cfg: Dict[str, Any]) -> Dict[str, int]:
    th = (cfg.get("thresholds") or {}) if cfg else {}
    return {
        "to_warming": max(1, int(th.get("initial_to_warming_exchanges", 4))),
        "to_intimate": max(1, int(th.get("warming_to_intimate_exchanges", 14))),
        "to_steady": max(1, int(th.get("intimate_to_steady_exchanges", 35))),
    }


def _stage_from_count(n: int, th: Dict[str, int]) -> str:
    if n >= th["to_steady"]:
        return "steady"
    if n >= th["to_intimate"]:
        return "intimate"
    if n >= th["to_warming"]:
        return "warming"
    return "initial"


def downgrade_from_user_text(
    st: Dict[str, Any],
    text: str,
    companion_cfg: Dict[str, Any],
) -> Optional[str]:
    """用户明确反感亲昵时降级。返回新阶段或 None。"""
    t = (text or "").strip().lower()
    if not t:
        return None
    kws: List[str] = list(companion_cfg.get("downgrade_keywords") or [])
    default_kws = ["别腻", "正经点", "别撩", "stop flirting", "不要撒娇", "严肃点"]
    for k in default_kws:
        if k not in kws:
            kws.append(k)
    hit = any(k.lower() in t for k in kws if k)
    if not hit:
        return None
    cur = (st.get("stage") or "initial").strip()
    if cur not in STAGE_ORDER:
        cur = "initial"
    if cur == "initial":
        return None
    ni = max(0, STAGE_ORDER.index(cur) - 1)
    new_s = STAGE_ORDER[ni]
    if new_s != cur:
        st["stage"] = new_s
        st["updated_at"] = time.time()
        # 若干轮内暂停仅靠轮次自动升阶，避免「刚说别腻又升回去」
        sup = int(companion_cfg.get("advance_suppress_after_downgrade", 8))
        st["suppress_advance_until"] = int(st.get("exchange_count", 0) or 0) + max(0, sup)
        return new_s
    return None


def reconcile_stage_after_assistant_reply(
    st: Dict[str, Any],
    companion_cfg: Dict[str, Any],
) -> Optional[str]:
    """
    在 exchange_count 已递增后调用：按阈值升阶（尊重 suppress_advance_until）。
    返回新阶段或 None。
    """
    if not companion_cfg.get("enabled", True):
        return None
    n = int(st.get("exchange_count", 0) or 0)
    sup = int(st.get("suppress_advance_until", 0) or 0)
    if n <= sup:
        return None
    th = _thresholds(companion_cfg)
    target = _stage_from_count(n, th)
    cur = (st.get("stage") or "initial").strip()
    if cur not in STAGE_ORDER:
        cur = "initial"
    ti, ci = STAGE_ORDER.index(target), STAGE_ORDER.index(cur)
    if ti > ci:
        st["stage"] = STAGE_ORDER[ti]
        st["updated_at"] = time.time()
        return st["stage"]
    return None


def _merge_natural_dialogue_cfg(raw: Any) -> Dict[str, Any]:
    """合并 natural_dialogue 默认与 YAML 覆盖。"""
    defaults: Dict[str, Any] = {
        "enabled": True,
        # exchange_count 为「已完成助手轮数」；≤ 此值时附加更严的「初面」约束
        "strict_exchange_max": 2,
        # 用户本条字符数（去空白）≤ 此值视为「短消息」，提示模型短回
        "short_user_chars": 22,
        # 用户消息命中以下子串时，提示偏事务/简洁语气（当轮覆盖）
        "work_like_keywords": [
            "订单", "单号", "通道", "费率", "回调", "查单", "代收", "代付",
            "bug", "上线", "发版", "会议", "deadline", "项目", "合同", "报价",
        ],
    }
    if not isinstance(raw, dict):
        return dict(defaults)
    merged = dict(defaults)
    merged.update(raw)
    return merged


def _user_message_is_short(text: str, max_chars: int) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    return len(t) <= max(1, int(max_chars))


def _user_message_work_like(text: str, kws: List[str]) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    for k in kws or []:
        ks = str(k).strip().lower()
        if ks and ks in t:
            return True
    return False


def build_natural_dialogue_prompt_addon(
    st: Dict[str, Any],
    companion_cfg: Dict[str, Any],
    *,
    user_message: str = "",
) -> str:
    """
    「先听懂再陪伴」：承接优先、克制假亲密、短消息镜像、事务型当轮降甜。
    注入在关系阶段块之后（仍属 conversion 域）。
    """
    if not companion_cfg.get("enabled", True):
        return ""
    nd = _merge_natural_dialogue_cfg(companion_cfg.get("natural_dialogue"))
    if not nd.get("enabled", True):
        return ""

    ex = int(st.get("exchange_count", 0) or 0)
    stage = (st.get("stage") or "initial").strip()
    strict_max = int(nd.get("strict_exchange_max", 2))
    short_lim = int(nd.get("short_user_chars", 22))
    work_kws = list(nd.get("work_like_keywords") or [])

    lines: List[str] = []
    lines.append("【对话自然化 · 优先于套路陪聊】")
    lines.append(
        "先接住用户本条消息里的具体词、问题或情绪，再展开；不要无视对方内容而先播人设或空泛寒暄。"
    )
    lines.append(
        "无上下文依据时：不要自称「专属」「小可爱」、不要用「悄悄话想跟我说」类亲密假设；"
        "少用「今天过得怎么样」等泛泛提问当开场，除非对方先聊日常。"
    )
    lines.append(
        "你是情感陪伴向，不是客服台：**禁止**「有什么可以帮您/帮您的吗」「需要什么服务」「请问有什么可以」等柜台话术；"
        "对方问「在吗」「在」时用一两句像私聊即可。"
    )
    lines.append(
        "句式上避免连续堆砌「呀～」「是不是…呀」同质化反问；陈述、接梗、偶尔一问交替即可。"
    )

    if ex <= strict_max or stage == "initial":
        lines.append(
            f"【本场约束 · 关系仍偏新（助手已完成轮数={ex}，阶段={STAGE_LABEL_ZH.get(stage, stage)}）】"
            "语气像正常私聊刚认识：克制撒娇与关系宣示，不要为了「可爱」而演。"
        )

    if _user_message_is_short(user_message, short_lim):
        lines.append(
            "【用户本轮偏短】你的回复也宜相对简短，不要硬凑多句或连环反问。"
        )

    if _user_message_work_like(user_message, work_kws):
        lines.append(
            "【本轮偏事务/信息】语气简洁平实，少用亲昵与撒娇句式，先把事说清楚。"
        )

    return "\n".join(lines)


def build_relationship_prompt_block(
    st: Dict[str, Any],
    companion_cfg: Dict[str, Any],
    *,
    ai_name: str = "",
    user_message: str = "",
) -> str:
    """注入到 AI 上下文的短提示（中文为主；非中文会话仍给中文指令由多语言规则覆盖）。"""
    if not companion_cfg.get("enabled", True):
        return ""
    stage = (st.get("stage") or "initial").strip()
    stages = companion_cfg.get("stages") or {}
    custom = (stages.get(stage) or {}) if isinstance(stages, dict) else {}
    zh = (custom.get("zh") or "").strip() if isinstance(custom, dict) else ""
    if not zh:
        zh = _default_stage_hint_zh(stage, ai_name)
    ex = int(st.get("exchange_count", 0) or 0)
    label = STAGE_LABEL_ZH.get(stage, stage)
    core = (
        f"【关系阶段 · {label}】累计有效互动约 {ex} 轮（助手侧计数）。{zh}"
    )
    addon = build_natural_dialogue_prompt_addon(
        st, companion_cfg, user_message=user_message
    )
    if addon:
        return core + "\n\n" + addon
    return core


def _default_stage_hint_zh(stage: str, ai_name: str) -> str:
    name = (ai_name or "你").strip() or "你"
    if stage == "initial":
        return (
            f"保持礼貌与自然距离，可轻度关心对方话题；不要一上来就过度亲昵或固定自称「小可爱」类话术。"
        )
    if stage == "warming":
        return (
            f"可逐步更放松、偶尔用昵称感语气，但仍观察对方反应；避免油腻与复读。"
        )
    if stage == "intimate":
        return (
            f"可更亲昵、有「互相熟悉」的语气，但仍遵守边界与合规；对方若冷淡则收敛。"
        )
    if stage == "steady":
        return (
            f"像稳定线上陪伴：自然接话、可适度撒娇与 callback 共同梗，避免戏剧化与空洞承诺。"
        )
    return f"以{name}的身份自然回复，注意与对方节奏一致。"
