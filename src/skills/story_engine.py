"""Phase ③/④ 剧情/场景 roleplay 引擎（确定性 + 配置驱动 + 分支多结局 + 完成回写）。

竞品对标星野/Talkie/筑梦岛：场景化剧情是陪伴高粘性玩法 + 天然付费解锁点（变现目录
早埋 ``story_ch1``/``all_story`` 占位）。Phase ③ 落地线性场景；Phase ④ 把这条
「记忆→成长→剧情」**单向链做成闭环**：

  剧情完成 → 回写一条「共享经历」到情景记忆（高置信）→ 被巩固为 stable +
  被 proactive_topic 日后主动回访（"还记得那次星空下的约定吗？"）→ 关系更深 →
  解锁更深剧情……记忆/成长/剧情互相喂养，形成正循环而非一条直线。

并补**分支多结局**：叙事 beat 可设 ``branch`` 选择点，按用户回应确定性路由到不同
``endings``，每个结局可带不同的回写记忆——给用户真实的「我的选择改变了故事」掌控感。

设计纪律（与 proactive_topic / relationship_level 一致）
──────────────────────────────────────────────────────
- **纯函数、零 IO/LLM/网络**：``state`` 可序列化（user_context["story_state"]）；
  回写动作由调用方（skill_manager）执行，引擎只**返回**完成记忆文本。
- **准入 gate 复用既有件**：付费 ``monetization.entitlement_allows`` + 关系 ``bond_level``。
- **推进/分支确定性**：轮次驱动 + 关键词匹配，无随机，可单测。

场景 schema（``config.companion.story.scenarios``）
──────────────────────────────────────────────────
    coffee_date:
      title: "初次咖啡约会"
      require_unlock: story_ch1     # 可空；走 entitlement_allows
      min_bond_level: 2             # 关系等级门槛（咬合 Phase ②）
      beats:
        - {id: arrive, directive: "..."}
        - {id: ask,    directive: "...要不要约下次？",       # 选择点
           branch: [{keywords: ["好","愿意","想"], ending: warm},
                    {keywords: ["算了","忙","不"],  ending: cool}],
           default_ending: warm}
      endings:                       # 多结局（可空；无则到末 beat 走 on_complete）
        warm: {directive: "结局：开心约好下次。", memory: "我们约好下次再一起喝咖啡"}
        cool: {directive: "结局：礼貌道别。",     memory: "我们一起喝过一次咖啡"}
      on_complete: {memory: "我们一起度过了一段时光"}   # 无分支/无结局时的兜底回写
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_ADVANCE_TURNS = 3


def _scenario(scenarios: Optional[Dict[str, Any]], sid: str) -> Optional[Dict[str, Any]]:
    if not isinstance(scenarios, dict):
        return None
    scn = scenarios.get(str(sid))
    return scn if isinstance(scn, dict) else None


def _beats(scn: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = scn.get("beats") or []
    return [b for b in raw if isinstance(b, dict) and (b.get("directive") or "").strip()]


def _endings(scn: Dict[str, Any]) -> Dict[str, Any]:
    e = scn.get("endings")
    return e if isinstance(e, dict) else {}


def scenario_locked_reason(
    scn: Dict[str, Any],
    *,
    entitlement: Optional[Dict[str, Any]] = None,
    bond_level: int = 0,
) -> str:
    """返回锁定原因 code（``""`` = 可进入）。

    - ``need_unlock:<feature>``：需付费解锁/会员授予（走 entitlement_allows）。
    - ``need_bond:<n>``：关系等级不足（先判，更友好）。
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
            "endings": len(_endings(scn)),
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
        "ending_id": "",       # 非空 = 已进入某结局段
        "turns_in_beat": 0,
        "started_at": ts,
        "updated_at": ts,
    }


def _match_branch(branch: Any, default_ending: str, user_message: str) -> str:
    """选择点路由：按用户回应关键词匹配 → 结局 id；无命中回 default_ending。"""
    msg = (user_message or "").lower()
    if isinstance(branch, list):
        for opt in branch:
            if not isinstance(opt, dict):
                continue
            kws = opt.get("keywords") or []
            for kw in kws:
                k = str(kw or "").strip().lower()
                if k and k in msg:
                    return str(opt.get("ending") or "").strip()
    return str(default_ending or "").strip()


def advance_state(
    state: Optional[Dict[str, Any]],
    scenarios: Optional[Dict[str, Any]],
    *,
    user_message: str = "",
    advance_turns: int = DEFAULT_ADVANCE_TURNS,
    now: Optional[float] = None,
) -> Tuple[Optional[Dict[str, Any]], bool, str]:
    """登记「一个用户轮次」并按需推进 beat / 路由分支 / 收场（确定性）。

    返回 ``(new_state, finished, memory)``：
      - 满 ``advance_turns`` 轮推进；遇 ``branch`` beat 按 ``user_message`` 路由到结局段。
      - ``finished=True`` 时 ``memory`` 为该结局（或 ``on_complete``）声明的「共享经历」回写
        文本（可空）；调用方据此写情景记忆。
      - state/场景非法 → ``(None, True, "")`` 安全降级。
    """
    if not isinstance(state, dict):
        return None, True, ""
    scn = _scenario(scenarios, str(state.get("scenario_id") or ""))
    if not scn:
        return None, True, ""
    beats = _beats(scn)
    if not beats:
        return None, True, ""

    at = max(1, int(advance_turns or DEFAULT_ADVANCE_TURNS))
    turns = int(state.get("turns_in_beat") or 0) + 1
    ts = float(now if now is not None else time.time())
    ending_id = str(state.get("ending_id") or "")
    endings = _endings(scn)

    # 已在结局段：演绎满 advance_turns 轮 → 收场并回写该结局记忆
    if ending_id:
        if turns >= at:
            mem = str((endings.get(ending_id) or {}).get("memory") or "").strip()
            return None, True, mem
        new_state = dict(state)
        new_state.update(turns_in_beat=turns, updated_at=ts)
        return new_state, False, ""

    if turns < at:
        new_state = dict(state)
        new_state.update(turns_in_beat=turns, updated_at=ts)
        return new_state, False, ""

    # 到点推进：先看当前 beat 是否为选择点
    idx = int(state.get("beat_index") or 0)
    idx = max(0, min(idx, len(beats) - 1))
    cur = beats[idx]
    branch = cur.get("branch")
    if branch:
        chosen = _match_branch(branch, cur.get("default_ending", ""), user_message)
        if chosen and chosen in endings:
            new_state = dict(state)
            new_state.update(ending_id=chosen, turns_in_beat=0, updated_at=ts)
            return new_state, False, ""
        # 分支未配妥 → 退化为线性收场

    # 线性推进 / 收场
    if idx + 1 < len(beats):
        new_state = dict(state)
        new_state.update(beat_index=idx + 1, turns_in_beat=0, updated_at=ts)
        return new_state, False, ""
    # 末 beat 之后：无分支结局 → on_complete 兜底回写
    mem = str((scn.get("on_complete") or {}).get("memory") or "").strip()
    return None, True, mem


def current_directive(
    state: Optional[Dict[str, Any]],
    scenarios: Optional[Dict[str, Any]],
) -> str:
    """当前导演指令（结局段取 endings[ending_id]，否则取当前 beat；越界 → ""）。"""
    if not isinstance(state, dict):
        return ""
    scn = _scenario(scenarios, str(state.get("scenario_id") or ""))
    if not scn:
        return ""
    ending_id = str(state.get("ending_id") or "")
    if ending_id:
        return str((_endings(scn).get(ending_id) or {}).get("directive") or "").strip()
    beats = _beats(scn)
    idx = int(state.get("beat_index") or 0)
    if idx < 0 or idx >= len(beats):
        return ""
    return str(beats[idx].get("directive") or "").strip()


def build_story_prompt_block(
    state: Optional[Dict[str, Any]],
    scenarios: Optional[Dict[str, Any]],
) -> str:
    """组装【剧情场景】prompt 块（无活动剧情 → ""）。"""
    directive = current_directive(state, scenarios)
    if not directive:
        return ""
    scn = _scenario(scenarios, str(state.get("scenario_id") or "")) or {}
    title = str(scn.get("title") or "").strip()
    in_ending = bool(str(state.get("ending_id") or ""))
    tag = "·结局" if in_ending else ""
    head = f"【剧情场景·{title}{tag}】" if title else "【剧情场景】"
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
