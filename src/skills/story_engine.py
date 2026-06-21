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


def _story_prereq_unmet(scn: Dict[str, Any], completed: Optional[Dict[str, str]]) -> str:
    """Phase ④续³ 跨场景前置：``requires_story`` 全部满足才放行，返回首个未满足的场景 id。

    schema（AND 语义）::

        requires_story:
          - {scenario: coffee_date}              # 完成过即可（任意结局）
          - {scenario: coffee_date, ending: warm} # 必须以 warm 结局完成

    ``completed`` = ``{scenario_id: ending_id_or_""}``（已完成剧情 → 所取结局）。
    """
    reqs = scn.get("requires_story")
    if not reqs:
        return ""
    done = completed if isinstance(completed, dict) else {}
    if isinstance(reqs, str):
        reqs = [{"scenario": reqs}]
    for cond in reqs:
        if isinstance(cond, str):
            cond = {"scenario": cond}
        if not isinstance(cond, dict):
            continue
        sid = str(cond.get("scenario") or "").strip()
        if not sid:
            continue
        if sid not in done:
            return sid
        need_ending = str(cond.get("ending") or "").strip()
        if need_ending and str(done.get(sid) or "") != need_ending:
            return sid
    return ""


def satisfied_prerequisite(
    scn: Dict[str, Any], completed: Optional[Dict[str, str]]
) -> Optional[Tuple[str, str]]:
    """返回该场景**已满足**的首个前置剧情 ``(scenario_id, ending_taken)``，无则 None。

    供「主动剧情邀约」织入个性化召回钩子：续作邀约里自然提起「上次我们一起经历的《前传》
    （以及那次的结局）」。仅在 ``select_story_invite`` 已判定可邀约（前置全满足）后调用，
    故 ``completed`` 里命中的即为满足项。``ending_taken`` 为用户当时所取结局 id（可空）。
    """
    reqs = scn.get("requires_story") if isinstance(scn, dict) else None
    if not reqs:
        return None
    done = completed if isinstance(completed, dict) else {}
    if isinstance(reqs, str):
        reqs = [{"scenario": reqs}]
    for cond in reqs:
        if isinstance(cond, str):
            cond = {"scenario": cond}
        if not isinstance(cond, dict):
            continue
        sid = str(cond.get("scenario") or "").strip()
        if sid and sid in done:
            return sid, str(done.get(sid) or "")
    return None


def ending_memory(scn: Dict[str, Any], ending_id: str) -> str:
    """取某场景某结局回写的「共享经历」文本（无结局/无 memory → on_complete 兜底 → ""）。

    供续作邀约引用前传那次的真实共同经历（"上次我们约好下次再一起喝咖啡…"），
    让召回有据可依、不空泛。纯读取，不抛。
    """
    if not isinstance(scn, dict):
        return ""
    eid = str(ending_id or "").strip()
    endings = scn.get("endings")
    if eid and isinstance(endings, dict):
        node = endings.get(eid)
        if isinstance(node, dict):
            mem = str(node.get("memory") or "").strip()
            if mem:
                return mem
    oc = scn.get("on_complete")
    if isinstance(oc, dict):
        return str(oc.get("memory") or "").strip()
    return ""


def scenario_locked_reason(
    scn: Dict[str, Any],
    *,
    entitlement: Optional[Dict[str, Any]] = None,
    bond_level: int = 0,
    completed: Optional[Dict[str, str]] = None,
) -> str:
    """返回锁定原因 code（``""`` = 可进入）。

    判定顺序（越友好/越可行动者优先）：关系 → 前置剧情 → 付费。
    - ``need_bond:<n>``：关系等级不足。
    - ``need_story:<scenario>``：需先经历某前置剧情（或以特定结局完成）。
    - ``need_unlock:<feature>``：需付费解锁/会员授予（走 entitlement_allows）。
    """
    try:
        min_bond = int(scn.get("min_bond_level") or 0)
    except (TypeError, ValueError):
        min_bond = 0
    if min_bond and int(bond_level or 0) < min_bond:
        return f"need_bond:{min_bond}"

    prereq = _story_prereq_unmet(scn, completed)
    if prereq:
        return f"need_story:{prereq}"

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
    completed: Optional[Dict[str, str]] = None,
) -> bool:
    return scenario_locked_reason(
        scn, entitlement=entitlement, bond_level=bond_level, completed=completed
    ) == ""


def list_scenarios(
    scenarios: Optional[Dict[str, Any]],
    *,
    entitlement: Optional[Dict[str, Any]] = None,
    bond_level: int = 0,
    completed: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """列出全部场景及其准入状态（供后台/MiniApp 展示「可玩 / 需解锁 / 需升级 / 需前传」）。"""
    out: List[Dict[str, Any]] = []
    if not isinstance(scenarios, dict):
        return out
    for sid, scn in scenarios.items():
        if not isinstance(scn, dict):
            continue
        reason = scenario_locked_reason(
            scn, entitlement=entitlement, bond_level=bond_level, completed=completed
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


def select_story_invite(
    scenarios: Optional[Dict[str, Any]],
    *,
    bond_level: int = 0,
    completed: Optional[Dict[str, str]] = None,
    active_id: str = "",
    entitlement: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """挑一个「现在就能开启、但用户还没经历过」的剧情作**主动邀约**（纯函数）。

    用于沉默期主动开场把新内容接进 re-engagement 闭环：剧情解锁 → 主动邀约 → 回流 →
    更多剧情。只邀约 **关系/前置已满足 + 当前权益可进入 + 未完成 + 非进行中** 的场景。

    - ``entitlement=None``（默认）→ 仅免费场景（``require_unlock`` 为空）通过；付费场景
      留给店内引导，不在沉默期隔空 teasing 锁住的内容。
    - ``completed``：``{scenario_id: ending}`` 已完成足迹（供 requires_story 判定 + 去重）。
    - ``active_id``：当前进行中的场景 id（跳过，避免「邀你开始你正在玩的」）。

    返回 ``{scenario_id, title}`` 或 ``None``（无可邀约/配置非法）。按 scenarios 声明顺序
    取第一个合格者（内容侧可借声明序表达推荐优先级）。
    """
    if not isinstance(scenarios, dict):
        return None
    done = completed if isinstance(completed, dict) else {}
    aid = str(active_id or "")
    for sid, scn in scenarios.items():
        if not isinstance(scn, dict):
            continue
        sid_s = str(sid)
        if sid_s in done or sid_s == aid:
            continue
        if not _beats(scn):           # 空场景无法开启 → 不邀约
            continue
        if scenario_available(
            scn, entitlement=entitlement, bond_level=bond_level, completed=done
        ):
            return {"scenario_id": sid_s, "title": str(scn.get("title") or sid_s)}
    return None


def start_scenario(
    scenario_id: str,
    scenarios: Optional[Dict[str, Any]],
    *,
    entitlement: Optional[Dict[str, Any]] = None,
    bond_level: int = 0,
    completed: Optional[Dict[str, str]] = None,
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """开启一个场景 → 初始 state（不可进入/未知/无 beat → None）。"""
    scn = _scenario(scenarios, scenario_id)
    if not scn or not _beats(scn):
        return None
    if not scenario_available(
        scn, entitlement=entitlement, bond_level=bond_level, completed=completed
    ):
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


def _finish_payload(d: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """从结局/on_complete 节点提取收场副作用：共享记忆文本 + 关系加成。"""
    d = d if isinstance(d, dict) else {}
    try:
        bonus = float(d.get("intimacy_bonus") or 0.0)
    except (TypeError, ValueError):
        bonus = 0.0
    return {
        "memory": str(d.get("memory") or "").strip(),
        "intimacy_bonus": bonus,
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
) -> Tuple[Optional[Dict[str, Any]], bool, Dict[str, Any]]:
    """登记「一个用户轮次」并按需推进 beat / 路由分支 / 收场（确定性）。

    返回 ``(new_state, finished, payload)``：
      - 满 ``advance_turns`` 轮推进；遇 ``branch`` beat 按 ``user_message`` 路由到结局段。
      - ``finished=True`` 时 ``payload`` = ``{"memory": str, "intimacy_bonus": float}``，
        取自该结局（或 ``on_complete``）声明的收场副作用：调用方据此回写情景记忆 +
        给关系加成（Phase ④ 把「剧情→记忆」「剧情→成长」两条边都接实）。
      - 未收场时 ``payload`` 为 ``{}``；state/场景非法 → ``(None, True, {})`` 安全降级。
    """
    if not isinstance(state, dict):
        return None, True, {}
    scn = _scenario(scenarios, str(state.get("scenario_id") or ""))
    if not scn:
        return None, True, {}
    beats = _beats(scn)
    if not beats:
        return None, True, {}

    at = max(1, int(advance_turns or DEFAULT_ADVANCE_TURNS))
    turns = int(state.get("turns_in_beat") or 0) + 1
    ts = float(now if now is not None else time.time())
    ending_id = str(state.get("ending_id") or "")
    endings = _endings(scn)

    # 已在结局段：演绎满 advance_turns 轮 → 收场并结算该结局副作用
    if ending_id:
        if turns >= at:
            return None, True, _finish_payload(endings.get(ending_id))
        new_state = dict(state)
        new_state.update(turns_in_beat=turns, updated_at=ts)
        return new_state, False, {}

    if turns < at:
        new_state = dict(state)
        new_state.update(turns_in_beat=turns, updated_at=ts)
        return new_state, False, {}

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
            return new_state, False, {}
        # 分支未配妥 → 退化为线性收场

    # 线性推进 / 收场
    if idx + 1 < len(beats):
        new_state = dict(state)
        new_state.update(beat_index=idx + 1, turns_in_beat=0, updated_at=ts)
        return new_state, False, {}
    # 末 beat 之后：无分支结局 → on_complete 兜底结算
    return None, True, _finish_payload(scn.get("on_complete"))


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
    "satisfied_prerequisite",
    "ending_memory",
    "list_scenarios",
    "select_story_invite",
    "start_scenario",
    "advance_state",
    "current_directive",
    "build_story_prompt_block",
]
