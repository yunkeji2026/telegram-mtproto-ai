"""Phase ③ 剧情/场景 roleplay 引擎（确定性 + 配置驱动）。

竞品对标星野/Talkie/筑梦岛：场景化剧情是陪伴的高粘性玩法，也是天然的付费解锁点
——变现目录早埋了 ``story_ch1`` / ``all_story`` 占位却**没有引擎驱动**。本模块补上：
把「场景剧本」声明在 config，按**关系等级 + 付费权益**双 gate 准入，按用户轮次**确定性
推进 beat**，每个 beat 产出一行【剧情场景】prompt 指令交回复层演绎（真实文案仍由 LLM 生成）。

设计纪律（与 proactive_topic / empathy_strategy / relationship_level 一致）
────────────────────────────────────────────────────────────────────
- **纯函数、零 IO、零 LLM、零网络**：``state`` 是可序列化 dict，由调用方持久化
  （如 ``user_context["story_state"]``，与 companion_relationship 同范式）。
- **准入 gate 复用既有件**：付费用 ``monetization.entitlement_allows``（不另造付费判定），
  关系深度用 relationship_level 的 ``bond_level``——剧情与「记忆→成长」链条天然咬合。
- **推进确定性**：每个用户轮次推进一格；本 beat 满 ``advance_turns`` 轮 → 进下一 beat；
  超出最后一个 beat → 剧终自动收场（state 清空）。无随机、可单测。

场景 schema（``config.companion.story.scenarios``）
──────────────────────────────────────────────────
    coffee_date:
      title: "初次咖啡约会"
      require_unlock: story_ch1   # 可空；catalog item / tier grant id（走 entitlement_allows）
      min_bond_level: 2           # 关系等级门槛（1 initial … 4 steady）；可空=0
      beats:
        - {id: arrive,  directive: "场景：约在安静的咖啡馆初次见面，自然描述环境与点单。"}
        - {id: chat,    directive: "场景推进：聊起彼此近况，气氛渐熟，多倾听多回应。"}
        - {id: closing, directive: "场景收尾：约会近尾声，自然表达期待下次再见，温柔道别。"}
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

# 默认每个 beat 推进所需的用户轮次（config 可覆盖 companion.story.advance_turns）。
DEFAULT_ADVANCE_TURNS = 3


def _scenario(scenarios: Optional[Dict[str, Any]], sid: str) -> Optional[Dict[str, Any]]:
    if not isinstance(scenarios, dict):
        return None
    scn = scenarios.get(str(sid))
    return scn if isinstance(scn, dict) else None


def _beats(scn: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = scn.get("beats") or []
    return [b for b in raw if isinstance(b, dict) and (b.get("directive") or "").strip()]


def scenario_locked_reason(
    scn: Dict[str, Any],
    *,
    entitlement: Optional[Dict[str, Any]] = None,
    bond_level: int = 0,
) -> str:
    """返回锁定原因 code（``""`` = 可进入）。

    - ``need_unlock:<feature>``：需付费解锁/会员授予该 feature（走 entitlement_allows）。
    - ``need_bond:<n>``：关系等级不足。
    付费判定**仅在 require_unlock 非空时**触发；不替代 monetization 侧的真实收费。
    """
    try:
        min_bond = int(scn.get("min_bond_level") or 0)
    except (TypeError, ValueError):
        min_bond = 0
    if min_bond and int(bond_level or 0) < min_bond:
        return f"need_bond:{min_bond}"

    feat = str(scn.get("require_unlock") or "").strip()
    if feat:
        from src.utils.monetization import entitlement_allows
        ent = entitlement or {"grants": (), "unlocked": ()}
        if not entitlement_allows(ent, feat):
            return f"need_unlock:{feat}"
    return ""


def scenario_available(
    scn: Dict[str, Any],
    *,
    entitlement: Optional[Dict[str, Any]] = None,
    bond_level: int = 0,
) -> bool:
    return scenario_locked_reason(
        scn, entitlement=entitlement, bond_level=bond_level
    ) == ""


def list_scenarios(
    scenarios: Optional[Dict[str, Any]],
    *,
    entitlement: Optional[Dict[str, Any]] = None,
    bond_level: int = 0,
) -> List[Dict[str, Any]]:
    """列出全部场景及其准入状态（供后台/MiniApp 展示「可玩 / 需解锁 / 需升级」）。"""
    out: List[Dict[str, Any]] = []
    if not isinstance(scenarios, dict):
        return out
    for sid, scn in scenarios.items():
        if not isinstance(scn, dict):
            continue
        reason = scenario_locked_reason(
            scn, entitlement=entitlement, bond_level=bond_level
        )
        out.append({
            "id": str(sid),
            "title": str(scn.get("title") or sid),
            "require_unlock": str(scn.get("require_unlock") or ""),
            "min_bond_level": int(scn.get("min_bond_level") or 0),
            "beats": len(_beats(scn)),
            "available": reason == "",
            "locked_reason": reason,
        })
    return out


def start_scenario(
    scenario_id: str,
    scenarios: Optional[Dict[str, Any]],
    *,
    entitlement: Optional[Dict[str, Any]] = None,
    bond_level: int = 0,
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """开启一个场景 → 初始 state（不可进入/未知/无 beat → None）。"""
    scn = _scenario(scenarios, scenario_id)
    if not scn or not _beats(scn):
        return None
    if not scenario_available(scn, entitlement=entitlement, bond_level=bond_level):
        return None
    ts = float(now if now is not None else time.time())
    return {
        "scenario_id": str(scenario_id),
        "beat_index": 0,
        "turns_in_beat": 0,
        "started_at": ts,
        "updated_at": ts,
    }


def advance_state(
    state: Optional[Dict[str, Any]],
    scenarios: Optional[Dict[str, Any]],
    *,
    advance_turns: int = DEFAULT_ADVANCE_TURNS,
    now: Optional[float] = None,
) -> Tuple[Optional[Dict[str, Any]], bool]:
    """登记「一个用户轮次已发生」并按需推进 beat（确定性）。

    返回 ``(new_state, finished)``：满 ``advance_turns`` 轮推进到下一 beat；超出最后一个
    beat → ``(None, True)`` 剧终。state/场景非法 → ``(None, True)``（视作结束，安全降级）。
    """
    if not isinstance(state, dict):
        return None, True
    scn = _scenario(scenarios, str(state.get("scenario_id") or ""))
    if not scn:
        return None, True
    beats = _beats(scn)
    if not beats:
        return None, True

    at = max(1, int(advance_turns or DEFAULT_ADVANCE_TURNS))
    idx = int(state.get("beat_index") or 0)
    turns = int(state.get("turns_in_beat") or 0) + 1
    ts = float(now if now is not None else time.time())

    if turns >= at:
        idx += 1
        turns = 0
        if idx >= len(beats):
            return None, True  # 剧终
    new_state = dict(state)
    new_state.update(beat_index=idx, turns_in_beat=turns, updated_at=ts)
    return new_state, False


def current_directive(
    state: Optional[Dict[str, Any]],
    scenarios: Optional[Dict[str, Any]],
) -> str:
    """当前 beat 的导演指令（state/场景非法或越界 → ""）。"""
    if not isinstance(state, dict):
        return ""
    scn = _scenario(scenarios, str(state.get("scenario_id") or ""))
    if not scn:
        return ""
    beats = _beats(scn)
    idx = int(state.get("beat_index") or 0)
    if idx < 0 or idx >= len(beats):
        return ""
    return str(beats[idx].get("directive") or "").strip()


def build_story_prompt_block(
    state: Optional[Dict[str, Any]],
    scenarios: Optional[Dict[str, Any]],
) -> str:
    """组装【剧情场景】prompt 块（无活动剧情 → ""）。

    交回复层在该场景设定下自然演绎；末尾约束保持陪伴口吻、跟随对方节奏，不旁白报幕。
    """
    directive = current_directive(state, scenarios)
    if not directive:
        return ""
    scn = _scenario(scenarios, str(state.get("scenario_id") or "")) or {}
    title = str(scn.get("title") or "").strip()
    head = f"【剧情场景·{title}】" if title else "【剧情场景】"
    return (
        f"{head}{directive}"
        "（沉浸在此情景里自然演绎，跟随对方节奏推进；别像旁白一样报幕或宣布章节。）"
    )


__all__ = [
    "DEFAULT_ADVANCE_TURNS",
    "scenario_locked_reason",
    "scenario_available",
    "list_scenarios",
    "start_scenario",
    "advance_state",
    "current_directive",
    "build_story_prompt_block",
]
