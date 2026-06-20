"""共享「回复大脑」上下文装配（N 线 核心1）。

A 线 (``src/client/telegram_client.py``) 与 B 线
(``src/integrations/protocol_autoreply.py``) 此前**各自**构造投喂
``SkillManager.process_message`` 的 context，导致两套漂移：A 线丰富、B 线仅 4 字段。

实况校准（以代码为准）：记忆(episodic, skill_manager L718)与情绪
(emotional_context, L729)其实由 **SkillManager 内部**按
``platform + user_id + chat_id`` 自动注入——只要这三者传对，两条线都能"有记忆有情绪"。
本模块负责把**平台无关**的标准键（平台/会话标识 + 人设路由 + 情绪 hint）收敛成
**可单测纯函数**，两条线都调用 → 同一套逻辑，改一处两边生效（不重复造轮子、防未来漂移）。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

# 群类会话（私聊以外）。channel 单独处理三级人设。
GROUP_CHAT_TYPES = ("group", "supergroup", "channel")


# ── Q3：关系事实源（intimacy / funnel）的进程级提供者 ────────────────────────
# 背景：A 线 ``TelegramClient`` 的构造早于 contacts 子系统 bootstrap（main.py 顺序
# 所限），无法在构造期注入 hooks。故用**惰性进程级 provider**：contacts 就绪后注册，
# client 在收到消息时按需读取，从而让主平台 Telegram(A 线) 也吃上与 RPA 各线相同的
# IntimacyEngine 事实源 → companion_relationship 双信号融合（沉默衰减/reunion）。
#
# provider 签名与 ``rpa_hooks.get_journey_intimacy/_funnel_stage`` 对齐（关键字参数
# ``channel/account_id/external_id``），直接复用 contacts hooks，不重复造轮子。
# 未注册 → ``resolve_*`` 返回 None → 行为完全等同旧版（向后兼容、零影响默认）。
_RelLookup = Callable[..., Any]
_REL_PROVIDERS: Dict[str, Optional[_RelLookup]] = {
    "intimacy": None,
    "funnel": None,
    "record": None,
}


def set_relationship_providers(
    *,
    intimacy_lookup: Optional[_RelLookup] = None,
    funnel_lookup: Optional[_RelLookup] = None,
    message_recorder: Optional[_RelLookup] = None,
) -> None:
    """注册关系事实源查询器/记录器（幂等；仅覆盖显式传入的项）。

    - ``intimacy_lookup`` / ``funnel_lookup``：只读查询（``rpa_hooks.telegram`` 开即注册）。
    - ``message_recorder``：把 Telegram 收/发写入 contacts → 生成 journey + 刷新 intimacy。
      **仅在显式开启** ``platform_login.telegram.contacts_recording`` 时注册；未注册时
      ``record_relationship_message`` 为 no-op → 默认零行为变化（遵循"新子系统默认关"）。
    """
    if intimacy_lookup is not None:
        _REL_PROVIDERS["intimacy"] = intimacy_lookup
    if funnel_lookup is not None:
        _REL_PROVIDERS["funnel"] = funnel_lookup
    if message_recorder is not None:
        _REL_PROVIDERS["record"] = message_recorder


def reset_relationship_providers() -> None:
    """测试钩子：清空已注册的关系事实源。"""
    _REL_PROVIDERS["intimacy"] = None
    _REL_PROVIDERS["funnel"] = None
    _REL_PROVIDERS["record"] = None


def record_relationship_message(
    account_id: Any,
    chat_key: Any,
    direction: str,
    *,
    channel: str = "telegram",
    text_preview: str = "",
    display_name: str = "",
) -> None:
    """把一条收/发消息记入 contacts（direction='in' 会刷新 intimacy_score）。

    未注册 recorder（默认）→ 静默 no-op；异常一律吞掉，绝不影响主消息链路。
    """
    fn = _REL_PROVIDERS.get("record")
    if fn is None or chat_key in (None, ""):
        return
    try:
        fn(
            channel=channel,
            account_id=str(account_id or "default"),
            external_id=str(chat_key),
            direction=str(direction or "in"),
            text_preview=(text_preview or "")[:120],
            display_name=str(display_name or ""),
        )
    except Exception:
        pass


def resolve_intimacy_score(
    account_id: Any, chat_key: Any, *, channel: str = "telegram"
) -> Optional[float]:
    """查当前会话的最新 intimacy_score（0-100）；无 provider/无值/异常 → None。"""
    fn = _REL_PROVIDERS.get("intimacy")
    if fn is None or chat_key in (None, ""):
        return None
    try:
        score = fn(
            channel=channel,
            account_id=str(account_id or "default"),
            external_id=str(chat_key),
        )
        return float(score) if score is not None else None
    except Exception:
        return None


def resolve_funnel_stage(
    account_id: Any, chat_key: Any, *, channel: str = "telegram"
) -> Optional[str]:
    """查当前会话的漏斗阶段（供 RelationshipStager 语气校准）；无则 None。"""
    fn = _REL_PROVIDERS.get("funnel")
    if fn is None or chat_key in (None, ""):
        return None
    try:
        st = fn(
            channel=channel,
            account_id=str(account_id or "default"),
            external_id=str(chat_key),
        )
        s = str(st).strip() if st is not None else ""
        return s or None
    except Exception:
        return None


def route_persona_id(
    account_persona_ids: Optional[List[Any]], chat_type: str = ""
) -> str:
    """3-tier 人设路由（与 A 线 ``_process_message_async`` 原逻辑等价）。

    - ``channel`` 且配了 ≥3 个人设 → 第 3 个（索引 2）
    - 群/超级群/频道 且配了 ≥2 个 → 第 2 个（索引 1）
    - 其余（含私聊）→ 第 1 个（索引 0）
    - 无人设 → 空串
    """
    ids = [str(p) for p in (account_persona_ids or []) if p]
    if not ids:
        return ""
    ct = str(chat_type or "").lower()
    if ct == "channel" and len(ids) > 2:
        return ids[2]
    if ct in GROUP_CHAT_TYPES and len(ids) > 1:
        return ids[1]
    return ids[0]


def emotion_hint(text: str, emotion_enhancer: Any = None) -> str:
    """安全包装 ``emotion_enhancer.analyze_message_emotion``；任何失败回退 ``neutral``。

    供 A 线复用（去掉内联 try/except）；B 线无 enhancer 时传 None → 返回 neutral，
    情绪仍由 SkillManager 内部 emotional_context 兜底，不影响"有情绪"。
    """
    if not text or emotion_enhancer is None:
        return "neutral"
    try:
        res = emotion_enhancer.analyze_message_emotion(text)
        return (res or {}).get("emotion", "neutral") or "neutral"
    except Exception:
        return "neutral"


def build_companion_context(
    *,
    platform: str,
    chat_id: Any,
    text: str = "",
    chat_type: str = "private",
    account_persona_ids: Optional[List[Any]] = None,
    persona_id: Optional[str] = None,
    emotion_enhancer: Any = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """组装投喂 ``SkillManager.process_message`` 的**标准平台无关** context。

    保证 A/B 两线在「平台标识 + 会话标识 + 人设路由 + 情绪 hint」上一致；
    记忆/情绪由 skill_manager 内部按 platform+user_id+chat_id 注入。

    Args:
        platform: 平台名（如 ``telegram``）；CrossPlatformIdentity / 记忆键依赖它。
        chat_id: 会话标识（A 线为 int chat id；B 线为 chat_key 字符串）。
        text: 当前用户文本（用于情绪粗判）。
        chat_type: ``private`` / ``group`` / ``supergroup`` / ``channel``。
        account_persona_ids: 账号配置的人设列表（A 线按 chat_type 路由）。
        persona_id: 显式单一人设（B 线 registry.meta），优先于列表路由。
        emotion_enhancer: 可选情绪分析器；None 则不带 hint（skill_manager 内部兜底）。
        extra: 调用方追加键（None 值会被跳过），如 contact_id/intimacy_score/request_id。
    """
    ct = str(chat_type or "private").lower() or "private"
    is_group = ct in GROUP_CHAT_TYPES
    pid = str(persona_id) if persona_id else route_persona_id(account_persona_ids, ct)
    ctx: Dict[str, Any] = {
        "platform": str(platform or ""),
        "chat_id": chat_id,
        "chat_type": ct,
        "is_group": is_group,
    }
    if pid:
        ctx["account_persona_id"] = pid
    _hint = emotion_hint(text, emotion_enhancer)
    if _hint and _hint != "neutral":
        ctx["user_emotion_hint"] = _hint
    if extra:
        for k, v in extra.items():
            if v is not None:
                ctx[k] = v
    return ctx


__all__ = [
    "route_persona_id",
    "emotion_hint",
    "build_companion_context",
    "set_relationship_providers",
    "reset_relationship_providers",
    "record_relationship_message",
    "resolve_intimacy_score",
    "resolve_funnel_stage",
]
