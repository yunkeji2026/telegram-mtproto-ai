"""深度人设（真人感）纯核心 — 5 层，全部纯函数、确定性、可单测、无 IO/无 LLM。

背景与研究依据（2026）：真人感的分水岭不是「记住事实」而是「记住经历 + 关系怎么演化
+ 人设自己有在过的生活 + 不用问就主动回指」。头部产品（Nomi>Replika>C.AI）的差距
正在这几点上。本模块把这些做成**确定性纯核心**（可复现、常驻门禁），LLM 增强留作可选。

分层（与 config `companion.deep_persona.*` 子开关一一对应）：
  L1 life_line     人设自传生活线（随真实日期推进、前后一致、可被回指）
  L2 relationship  关系画像 L5（我眼中的 TA + 我们怎么走过来的）+ 不问就回指
  L3 tastes        口味/观点账本（稳定喜好立场，防"一味附和"的假感）+ inside_jokes 内部梗
  L4 experiential  经历式记忆（事件+情感+叙事，情绪浓的优先，可叙事式回指）
  L5 texture       拟人细节（时空锚定 + 刻意不完美，熟络阶段才开）

所有函数：脏输入安全退化为 ""/None/[]，绝不抛异常给主链路。
安全边界（由调用方与既有安全链共同保证）：对方情绪低落/危机时不抖机灵、不回指伤心事。
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

# 熟络阶段（脾气/不完美/内部梗等"有棱角"行为的闸门，与 persona_manager 同口径）。
_INTIMATE_STAGES = ("intimate", "steady")
_WARMISH_STAGES = ("warming", "intimate", "steady")


def _stable_hash(text: str) -> int:
    """确定性哈希（跨进程稳定，不用内置 hash 的随机化）。"""
    return int(hashlib.sha1(str(text or "").encode("utf-8")).hexdigest(), 16)


def _as_list(v: Any) -> List[str]:
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        return [v.strip()]
    return []


# ── L1 · 人设自传生活线 ──────────────────────────────────────────────────────

def pick_life_beat(
    persona: Dict[str, Any], now: datetime, *, seed_salt: str = "", stride_days: int = 3
) -> Optional[str]:
    """确定性挑选"人设最近的生活片段"（一条向前走、前后一致的生活线）。

    从 ``persona['life_arc']``（list[str] 具体生活事件池，或 dict{theme, beats, stride_days}）
    选取：以 ``stride_days`` 天为一个「生活阶段」窗（默认 3 天），窗内**稳定不变**（避免
    "昨天说考完试今天又在备考"的自相矛盾），跨窗按 **人设偏移 + 阶段序号**推进轮转。
    无 life_arc → None（不注入，行为不变）。
    """
    if not isinstance(persona, dict):
        return None
    arc = persona.get("life_arc")
    beats: List[str] = []
    if isinstance(arc, dict):
        beats = _as_list(arc.get("beats"))
        try:
            stride_days = int(arc.get("stride_days") or stride_days)
        except (TypeError, ValueError):
            pass
    else:
        beats = _as_list(arc)
    if not beats:
        return None
    stride = max(1, int(stride_days or 1))
    try:
        day_ord = now.date().toordinal()
    except Exception:
        return beats[0]
    pid = str(persona.get("id") or persona.get("name") or "")
    offset = _stable_hash(pid + "|" + str(seed_salt)) % len(beats)
    phase = day_ord // stride
    idx = (phase + offset) % len(beats)
    return beats[idx]


def format_life_context(beat: Optional[str], *, theme: str = "") -> str:
    """L1 生活线 → 提示词块。空 beat → ""。"""
    beat = str(beat or "").strip()
    if not beat:
        return ""
    theme_s = str(theme or "").strip()
    head = f"【你最近的生活】{beat}"
    if theme_s:
        head += f"（这阵子你的主线：{theme_s}）"
    return (
        head + "——这是你真实在过的日子，可在合适时自然带出或在被问近况时提起；"
        "**前后保持一致**，不要编造与你人设背景冲突的事，也别硬塞进每句话。"
    )


def life_theme(persona: Dict[str, Any]) -> str:
    arc = (persona or {}).get("life_arc") if isinstance(persona, dict) else None
    if isinstance(arc, dict):
        return str(arc.get("theme") or "").strip()
    return ""


# ── L2 · 关系画像 L5 + 不问就回指 ────────────────────────────────────────────

def build_relationship_profile(
    *,
    display_name: str = "",
    stable_facts: Optional[Sequence[str]] = None,
    dominant_emotion: str = "",
    milestones: Optional[Sequence[str]] = None,
    cares_about: Optional[Sequence[str]] = None,
    sensitive: Optional[Sequence[str]] = None,
    max_chars: int = 200,
) -> str:
    """把结构化输入巩固成一段"我眼中的 TA + 我们怎么走过来的"关系画像（≤max_chars）。

    纯组装（TiMem L5 思想的轻量落地）：不调 LLM，可复现。上层可后续用 LLM 精修，
    但门禁走这条确定性路径。空输入 → ""。
    """
    facts = _as_list(stable_facts)
    ms = _as_list(milestones)
    cares = _as_list(cares_about)
    sens = _as_list(sensitive)
    name = str(display_name or "对方").strip() or "对方"
    parts: List[str] = []
    if facts:
        parts.append(f"关于{name}：" + "；".join(facts[:4]))
    if cares:
        parts.append("TA 在意：" + "、".join(cares[:3]))
    de = str(dominant_emotion or "").strip()
    if de and de not in ("平稳", "neutral", "简短"):
        parts.append(f"最近情绪基调偏「{de}」")
    if ms:
        parts.append("你们的共同点滴：" + "；".join(ms[:2]))
    if sens:
        parts.append("雷区（小心）：" + "、".join(sens[:2]))
    if not parts:
        return ""
    text = "。".join(parts)
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip("；、。") + "…"
    return text


def format_relationship_profile(profile: str) -> str:
    profile = str(profile or "").strip()
    if not profile:
        return ""
    return (
        "【关系画像·你对 TA 的了解】" + profile
        + "——把 TA 当成你真的认识、一路聊过来的人；自然体现这份熟悉，别机械复述这些信息。"
    )


def build_callback_opener(
    open_loops: Optional[Sequence[Dict[str, Any]]],
    now: datetime,
    *,
    stage: str = "",
    min_days: float = 1.0,
    max_days: float = 30.0,
    suppress: bool = False,
    roll: float = 0.0,
    probability: float = 1.0,
) -> Optional[str]:
    """从"未收尾话题"里挑一个，生成一句**不用问就主动回指**的开场。

    open_loops: [{topic, ts(datetime|iso), emotion, salience}]。挑选规则：距今在
    [min_days, max_days] 窗内、情绪/salience 最高的一条。``suppress=True``（对方当下
    负面/危机，由调用方判定）→ 直接 None（脆弱时不翻旧账）。无合适 → None。

    **偶发闸**：``roll >= probability`` → None——避免每条回复都追问"后来怎么样了"，
    让回指像真人一样偶尔自然发生（默认 probability=1.0 即不限制，由调用方传 roll/概率控制）。
    """
    if suppress or not open_loops:
        return None
    try:
        if float(roll) >= float(probability):
            return None
    except (TypeError, ValueError):
        pass
    stage_l = (stage or "").strip().lower()
    # 生人阶段（initial）不主动翻共同往事，显得突兀
    if stage_l and stage_l not in _WARMISH_STAGES:
        return None
    best = None
    best_score = -1.0
    for lp in open_loops:
        if not isinstance(lp, dict):
            continue
        topic = str(lp.get("topic") or "").strip()
        if not topic:
            continue
        ts = lp.get("ts")
        days = _days_since(ts, now)
        if days is None or days < min_days or days > max_days:
            continue
        try:
            sal = float(lp.get("salience") or 0.0)
        except (TypeError, ValueError):
            sal = 0.0
        # 情绪浓 + 久一点 → 更值得回指（REMT 情感权重思想）
        score = sal + min(days / max_days, 1.0) * 0.5
        if score > best_score:
            best_score = score
            best = topic
    if not best:
        return None
    return f"（可自然地不问自提，回指共同往事）你上次提到「{best}」，后来怎么样了？"


def _days_since(ts: Any, now: datetime) -> Optional[float]:
    if ts is None:
        return None
    dt: Optional[datetime] = None
    if isinstance(ts, datetime):
        dt = ts
    elif isinstance(ts, (int, float)):
        try:
            dt = datetime.fromtimestamp(float(ts))
        except Exception:
            return None
    elif isinstance(ts, str):
        s = ts.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except Exception:
            return None
    if dt is None:
        return None
    try:
        if dt.tzinfo is not None and now.tzinfo is None:
            dt = dt.replace(tzinfo=None)
        return max(0.0, (now - dt).total_seconds() / 86400.0)
    except Exception:
        return None


# ── L3 · 口味/观点账本 + 内部梗 ───────────────────────────────────────────────

def format_tastes(persona: Dict[str, Any]) -> str:
    """从 ``persona['tastes']`` = {likes,dislikes,opinions} 生成"稳定口味/立场"提示。

    目的：治 Replika"一味附和、没立场"的假感——人设有主见、有喜恶，跨对话不自相矛盾。
    """
    if not isinstance(persona, dict):
        return ""
    t = persona.get("tastes")
    if not isinstance(t, dict):
        return ""
    likes = _as_list(t.get("likes"))
    dislikes = _as_list(t.get("dislikes"))
    opinions = _as_list(t.get("opinions"))
    segs: List[str] = []
    if likes:
        segs.append("喜欢：" + "、".join(likes[:6]))
    if dislikes:
        segs.append("不喜欢：" + "、".join(dislikes[:6]))
    if opinions:
        segs.append("你的一些看法：" + "；".join(opinions[:4]))
    if not segs:
        return ""
    return (
        "【你的稳定口味与立场】" + "。".join(segs)
        + "——这些是你真实、稳定的偏好，**跨对话保持一致不自相矛盾**；"
        "你有自己的主见，该赞同就赞同、该有不同意见就坦诚说，不必一味附和讨好。"
    )


# 内部梗检测：分词很轻（中文按 2-6 字滑窗 + 英文 token），过滤高频停用。
_STOP = frozenset([
    "什么", "怎么", "这个", "那个", "我们", "你们", "然后", "就是", "还有", "但是",
    "可以", "不是", "这样", "知道", "觉得", "真的", "一个", "有点", "哈哈", "嗯嗯",
    "the", "and", "you", "that", "this", "for", "are", "was", "with", "have",
])


def detect_recurring_phrases(
    messages: Sequence[str],
    *,
    min_count: int = 3,
    min_len: int = 2,
    max_len: int = 8,
    top_k: int = 5,
) -> List[str]:
    """从历史消息里检出**反复出现的短语/昵称/私语**（内部梗候选）。

    纯启发式：中文取 2..max_len 字滑窗、英文取单词，统计跨消息出现次数（同一条只计一次），
    过滤停用词与纯标点，返回出现≥min_count 的 top_k。空/异常 → []。
    """
    if not messages:
        return []
    counts: Dict[str, int] = {}
    for msg in messages:
        s = str(msg or "").strip()
        if not s:
            continue
        seen_in_msg = set()
        # 英文/数字 token
        for tok in re.findall(r"[A-Za-z][A-Za-z0-9']{2,}", s):
            k = tok.lower()
            if k in _STOP or len(k) < 3:
                continue
            seen_in_msg.add(k)
        # 中文滑窗
        han = re.findall(r"[\u4e00-\u9fff]+", s)
        for run in han:
            n = len(run)
            for L in range(min_len, min(max_len, n) + 1):
                for i in range(0, n - L + 1):
                    frag = run[i : i + L]
                    if frag in _STOP:
                        continue
                    seen_in_msg.add(frag)
        for k in seen_in_msg:
            counts[k] = counts.get(k, 0) + 1
    # 取高频；对中文优先较长的片段（更像"梗"而非碎词），做一次包含去冗
    cand = sorted(
        (k for k, c in counts.items() if c >= min_count),
        key=lambda k: (counts[k], len(k)), reverse=True,
    )
    picked: List[str] = []
    for k in cand:
        if any((k in p or p in k) for p in picked):
            continue
        picked.append(k)
        if len(picked) >= top_k:
            break
    return picked


def format_inside_jokes(jokes: Sequence[str]) -> str:
    js = _as_list(jokes)
    if not js:
        return ""
    return (
        "【你们之间的默契/梗】" + "、".join(f"「{j}」" for j in js[:5])
        + "——这些是你们聊出来的专属梗/口头语，合适时自然复用会显得很熟；别生硬堆砌。"
    )


# ── L4 · 经历式记忆（事件+情感+叙事）────────────────────────────────────────

def rank_by_affect(events: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按情感强度降序（REMT：情绪浓的经历更该被记住/回指）。"""
    def _key(e: Dict[str, Any]) -> float:
        try:
            return float(e.get("salience") or e.get("intensity") or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return sorted(
        [e for e in (events or []) if isinstance(e, dict) and e.get("what")],
        key=_key, reverse=True,
    )


def _token_relevance(query_text: str, event_text: str) -> float:
    """默认字面相关度：中文 2-gram + 英文词 的重叠度，归一到 [0,1]。"""
    q = _tokens(query_text)
    if not q:
        return 0.0
    ov = len(_tokens(event_text) & q)
    return min(ov / 3.0, 1.0)


def select_experiential(
    events: Sequence[Dict[str, Any]], *, now: datetime, query_text: str = "",
    top_k: int = 3, half_life_days: float = 14.0,
    sim_fn: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """C1（REMT）：情感 × 时近 × 与当前话题相关 的加权召回。

    score = 0.6·salience + 0.25·recency(指数衰减) + 0.45·relevance。
    让「和当下话题相关且情绪浓」的经历优先浮现，避免总回指同几件事。query_text 空 →
    退化为 情感×时近（不含相关项）。纯函数。

    D3 语义缝：``sim_fn(query_text, event_text) -> float in [0,1]`` 可注入（如向量余弦），
    不传则用确定性字面相关（``_token_relevance``）。任何 sim_fn 异常 → 回落字面（零阻断）。
    """
    _sim = sim_fn if callable(sim_fn) else _token_relevance
    q = str(query_text or "")
    scored = []
    for e in events or []:
        if not isinstance(e, dict) or not e.get("what"):
            continue
        try:
            sal = float(e.get("salience") or e.get("intensity") or 0.0)
        except (TypeError, ValueError):
            sal = 0.0
        days = _days_since(e.get("ts"), now)
        recency = 0.5 ** (days / max(1e-6, half_life_days)) if days is not None else 0.3
        rel = 0.0
        if q:
            try:
                rel = float(_sim(q, str(e.get("what") or "")))
            except Exception:
                rel = _token_relevance(q, str(e.get("what") or ""))
            rel = max(0.0, min(1.0, rel))
        score = 0.6 * sal + 0.25 * recency + 0.45 * rel
        scored.append((score, e))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in scored[:top_k]]


def life_share_allowed(
    recent_shares: Optional[Sequence[float]], now: datetime, *,
    max_per_week: int = 2, min_gap_hours: float = 48.0,
) -> bool:
    """D2 反打扰：主动分享生活线是否允许（防"天天主动汇报生活"的机械感）。

    recent_shares: 过去主动分享的时间戳（epoch 秒）列表。规则：
      - 近 7 天分享次数 < max_per_week；
      - 距上次分享 ≥ min_gap_hours。
    两者皆满足才允许。空历史 → 允许。纯函数。
    """
    try:
        now_ts = now.timestamp()
    except Exception:
        return True
    ts = [float(t) for t in (recent_shares or []) if isinstance(t, (int, float))]
    if not ts:
        return True
    week_ago = now_ts - 7 * 86400
    recent_week = [t for t in ts if t >= week_ago]
    if len(recent_week) >= int(max_per_week):
        return False
    last = max(ts)
    if (now_ts - last) < float(min_gap_hours) * 3600:
        return False
    return True


def life_share_time_ok(
    now: datetime, *, quiet_start_hour: int = 0, quiet_end_hour: int = 8,
) -> bool:
    """E5：主动分享的时段闸——静默时段（默认 0-8 点深夜/清晨）不主动打扰。

    quiet_start<quiet_end：区间内静默；start>end（跨午夜）：区间外静默。纯函数。
    """
    if not isinstance(now, datetime):
        return True
    h = now.hour
    qs, qe = int(quiet_start_hour) % 24, int(quiet_end_hour) % 24
    if qs == qe:
        return True
    if qs < qe:
        in_quiet = qs <= h < qe
    else:
        in_quiet = h >= qs or h < qe
    return not in_quiet


def cosine_sim(a: Sequence[float], b: Sequence[float]) -> float:
    """E1：余弦相似度 [−1,1]，脏输入/零向量 → 0.0。纯函数。"""
    try:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(float(x) * float(y) for x, y in zip(a, b))
        na = sum(float(x) * float(x) for x in a) ** 0.5
        nb = sum(float(y) * float(y) for y in b) ** 0.5
        if na <= 0 or nb <= 0:
            return 0.0
        return dot / (na * nb)
    except Exception:
        return 0.0


def make_embedding_sim_fn(query_emb: Sequence[float], text_to_emb: Dict[str, Sequence[float]]):
    """E1：构造 sim_fn(query_text, event_text) → 余弦(query_emb, event_emb)（映射 [−1,1]→[0,1]）。

    ``text_to_emb`` 是 事件文本→其向量 的映射（调用方从 store 缓存取）。query_emb 空或
    某事件无向量 → 该项相似度 0（select_experiential 会自然回落到情感×时近）。
    返回的闭包可直接传给 ``select_experiential(sim_fn=...)``。
    """
    q = list(query_emb or [])

    def _sim(_query_text: str, event_text: str) -> float:
        emb = text_to_emb.get(str(event_text or ""))
        if not q or not emb:
            return 0.0
        c = cosine_sim(q, emb)
        return max(0.0, (c + 1.0) / 2.0) if c > 0 else 0.0

    return _sim


def refine_profile_llm(
    profile: str, persona: Dict[str, Any], *, llm_fn: Optional[Any] = None,
) -> str:
    """D1 可选：用 LLM 把确定性关系画像润色得更自然，**过守卫后才采用**。

    ``llm_fn(prompt) -> str`` 可注入（真实接 ai_client；测试注入假函数）。安全：
    润色结果若命中人设漂移（forbidden/topics_to_avoid）或为空 → **回落原确定性画像**。
    纯函数（IO 经注入的 llm_fn）。不传 llm_fn → 原样返回。
    """
    base = str(profile or "").strip()
    if not base or not callable(llm_fn):
        return base
    try:
        prompt = (
            "把下面这段「关系画像」改写得更像一个真人私下的自然描述，"
            "保留全部事实与情绪基调，不要新增任何未提及的信息，只输出改写结果：\n\n" + base
        )
        out = str(llm_fn(prompt) or "").strip()
    except Exception:
        return base
    if not out:
        return base
    if detect_persona_drift(persona or {}, profile=out):
        return base
    return out[:400]


def to_experiential_recall(events: Sequence[Dict[str, Any]], *, top_k: int = 3) -> str:
    """经历（事件+情感）→ 叙事式召回块。区别于"事实条目"：带情感、可被叙事回指。

    events: [{what, emotion, when, salience}]。"记得故事而非事实"（Max 公园跑丢那次
    vs 有条狗叫 Max）。空 → ""。
    """
    ranked = rank_by_affect(events)[:top_k]
    if not ranked:
        return ""
    lines: List[str] = []
    for e in ranked:
        what = str(e.get("what") or "").strip()
        emo = str(e.get("emotion") or "").strip()
        when = str(e.get("when") or "").strip()
        seg = what
        if when:
            seg = f"（{when}）{seg}"
        if emo and emo not in ("平稳", "neutral"):
            seg += f"——当时 TA 的情绪是「{emo}」"
        lines.append("- " + seg)
    return (
        "【你们一起经历过的事（带着当时的心情去记）】\n" + "\n".join(lines)
        + "\n合适时可**叙事式回指**（如「还记得…那次吗」），体现你记得的是经历和感受，不只是事实。"
    )


# ── L5 · 拟人细节：时空锚定 + 刻意不完美 ─────────────────────────────────────

_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def temporal_anchor(now: datetime) -> str:
    """从当前时间派生"时段/星期"锚点提示（让回复像真人一样有时间感）。"""
    if not isinstance(now, datetime):
        return ""
    h = now.hour
    if h < 5:
        seg = "深夜"
    elif h < 8:
        seg = "清晨"
    elif h < 11:
        seg = "上午"
    elif h < 13:
        seg = "中午"
    elif h < 17:
        seg = "下午"
    elif h < 19:
        seg = "傍晚"
    elif h < 23:
        seg = "晚上"
    else:
        seg = "深夜"
    try:
        wd = _WEEKDAY_CN[now.weekday()]
    except Exception:
        wd = ""
    when = f"{wd}{seg}" if wd else seg
    hint = f"【此刻】现在大约是{when}"
    if h < 5 or h >= 23:
        hint += "，很晚了"
    if now.weekday() == 0 and h < 12:
        hint += "（周一上午，很多人会有点丧）"
    if now.weekday() >= 5:
        hint += "（周末）"
    return (
        hint + "——回复时带一点自然的时间感（如问对方吃了没、这么晚还没睡呀），"
        "但别机械报时、别每句都提。"
    )


def maybe_imperfection_hint(
    *, enabled: bool, stage: str, roll: float, probability: float = 0.15
) -> str:
    """刻意不完美（打字修正/在忙/坦承不知道）——熟络阶段 + 概率触发才注入。

    ``roll`` 由调用方传入（如 random.random()），保持本函数纯/可测；roll<probability 才触发。
    """
    if not enabled:
        return ""
    if (stage or "").strip().lower() not in _INTIMATE_STAGES:
        return ""
    try:
        if float(roll) >= float(probability):
            return ""
    except (TypeError, ValueError):
        return ""
    return (
        "【真人小瑕疵·本条可用】像真人打字一样，偶尔可以有一点点不完美："
        "打错字随手补一句「*打错了」、正忙时说「等下哈我手上有点事」、"
        "不知道的就坦率说「这我还真不清楚欸」——自然即可，绝不影响安全与正事。"
    )


# ── 总装配 ───────────────────────────────────────────────────────────────────

def _flag(cfg: Dict[str, Any], key: str) -> bool:
    if not isinstance(cfg, dict):
        return False
    return bool(cfg.get(key, False))


def build_deep_persona_block(
    persona: Dict[str, Any],
    *,
    now: datetime,
    cfg: Optional[Dict[str, Any]] = None,
    stage: str = "",
    deep_ctx: Optional[Dict[str, Any]] = None,
    imperfection_roll: float = 1.0,
) -> str:
    """按 config 子开关组装深度人设增强块（注入系统提示词）。

    ``cfg`` = config `companion.deep_persona`（含 enabled + 各层布尔）。
    ``deep_ctx`` = 运行期数据（store/history 取来）：{relationship_profile, inside_jokes,
    experiential_events, open_loops, suppress_callbacks}。缺省则相应层只出"可由 persona
    静态派生"的部分。整体 enabled=false → 返回 ""（零行为变更）。
    """
    cfg = cfg or {}
    if not cfg.get("enabled", False):
        return ""
    persona = persona or {}
    deep_ctx = deep_ctx or {}
    blocks: List[str] = []

    # L1 生活线（persona.life_arc 静态派生，无需 store）
    if _flag(cfg, "life_line"):
        beat = pick_life_beat(persona, now)
        lc = format_life_context(beat, theme=life_theme(persona))
        if lc:
            blocks.append(lc)

    # L3 口味/立场（persona.tastes 静态）
    if _flag(cfg, "tastes"):
        tb = format_tastes(persona)
        if tb:
            blocks.append(tb)

    # L2 关系画像（运行期）
    if _flag(cfg, "relationship"):
        rp = format_relationship_profile(str(deep_ctx.get("relationship_profile") or ""))
        if rp:
            blocks.append(rp)
        cb = build_callback_opener(
            deep_ctx.get("open_loops"), now, stage=stage,
            suppress=bool(deep_ctx.get("suppress_callbacks")),
            roll=float(deep_ctx.get("callback_roll", 0.0) or 0.0),
            probability=float(cfg.get("callback_probability", 0.35) or 0.35),
        )
        if cb:
            blocks.append(cb)

    # L3b 内部梗（运行期）
    if _flag(cfg, "inside_jokes"):
        ij = format_inside_jokes(deep_ctx.get("inside_jokes") or [])
        if ij:
            blocks.append(ij)

    # L4 经历式记忆（运行期）— C1：带 query_text 时走 情感×时近×相关 加权召回
    if _flag(cfg, "experiential"):
        _events = deep_ctx.get("experiential_events") or []
        _q = str(deep_ctx.get("query_text") or "")
        if _q and _events:
            _events = select_experiential(
                _events, now=now, query_text=_q,
                sim_fn=deep_ctx.get("experiential_sim_fn"))  # E1 语义 sim（缺则字面）
        ex = to_experiential_recall(_events)
        if ex:
            blocks.append(ex)

    # E3 人设自身长期记忆（跨会话去标识见闻；默认关）——deep_ctx 传入已取好的 top 话题
    if _flag(cfg, "self_memory"):
        from src.companion.persona_self_memory import format_self_memory
        sm = format_self_memory(deep_ctx.get("self_topics") or [])
        if sm:
            blocks.append(sm)

    # L5 拟人细节（now 派生 + 概率不完美）
    if _flag(cfg, "texture"):
        ta = temporal_anchor(now)
        if ta:
            blocks.append(ta)
        imp = maybe_imperfection_hint(
            enabled=True, stage=stage, roll=imperfection_roll,
            probability=float(cfg.get("imperfection_probability", 0.15) or 0.15),
        )
        if imp:
            blocks.append(imp)

    if not blocks:
        return ""
    return "【深度人设增强】\n" + "\n".join(blocks)


# ── Wave-Next-2 C2 · 生活线主动分享开场（纯核心）─────────────────────────────

def build_life_beat_opener(
    persona: Dict[str, Any], now: datetime, *, gate: str = "",
) -> Dict[str, Any]:
    """把人设"最近的生活片段"做成一条**主动分享**开场（像真人主动说近况）。

    gate=="block"（近期危机）→ {}；其余允许（温和分享，soft 也可）。无 life_arc → {}。
    返回 opener dict：``{mode:"life_share", fact, directive}``，与 build_proactive_opener 同形。
    """
    if str(gate or "").strip().lower() == "block":
        return {}
    beat = pick_life_beat(persona, now)
    if not beat:
        return {}
    return {
        "mode": "life_share",
        "fact": beat,
        "directive": (
            f"像真人主动报近况一样，自然地跟对方分享你最近生活里的这件事：「{beat}」——"
            "口语、简短、有情绪，说完自然把话题抛回给对方（问问 TA 最近怎样），"
            "不要像念稿，也不要客服腔。"
        ),
        "silent_hours": 0.0,
    }


# ── Wave-Next A · 巩固编排 + open_loop 自动收尾（纯核心）────────────────────────

def build_profile_from_signals(
    *,
    display_name: str = "",
    inbound_texts: Optional[Sequence[str]] = None,
    dominant_emotion: str = "",
    experiential: Optional[Sequence[Dict[str, Any]]] = None,
    max_chars: int = 200,
) -> str:
    """从运行期信号巩固关系画像 L5（纯函数，确定性）。

    信号来源（调用方从 store 取）：
      - inbound_texts：客户近期消息（提取"反复出现的话题"作代理事实/在意点）
      - dominant_emotion：conversation_meta.last_emotion（最近情绪基调）
      - experiential：经历库（情绪浓的共同经历 → 里程碑）
    组装走既有 ``build_relationship_profile``（TiMem L5 思想的确定性落地）。
    """
    topics = detect_recurring_phrases(
        list(inbound_texts or []), min_count=3, min_len=2, max_len=8, top_k=6)
    ranked_exp = rank_by_affect(list(experiential or []))
    milestones = [str(e.get("what") or "").strip()
                  for e in ranked_exp[:2] if e.get("what")]
    # 话题拆两半：前几条作"关于TA的事实"，其余作"在意点"，避免堆在一处
    facts = topics[:3]
    cares = topics[3:6]
    return build_relationship_profile(
        display_name=display_name, stable_facts=facts,
        dominant_emotion=dominant_emotion, milestones=milestones,
        cares_about=cares, max_chars=max_chars,
    )


def find_resolved_loops(
    open_loops: Optional[Sequence[Dict[str, Any]]],
    recent_text: str,
    *,
    min_overlap: int = 2,
) -> List[str]:
    """检测哪些未收尾话题已被后续消息"回应"（可标记收尾，避免反复追问同一件事）。

    纯启发式：新消息与某 open_loop 的 topic 有足够的中文片段/英文词重叠 → 视为已回应。
    返回应收尾的 topic 列表。空/无匹配 → []。
    """
    txt = str(recent_text or "").strip()
    if not txt or not open_loops:
        return []
    txt_tokens = _tokens(txt)
    resolved: List[str] = []
    for lp in open_loops:
        if not isinstance(lp, dict):
            continue
        topic = str(lp.get("topic") or "").strip()
        if not topic:
            continue
        overlap = len(_tokens(topic) & txt_tokens)
        if overlap >= min_overlap:
            resolved.append(topic)
    return resolved


def _tokens(text: str) -> set:
    """粗分词集合：中文 2-gram + 英文词（用于话题重叠判断）。"""
    s = str(text or "")
    out = set()
    for tok in re.findall(r"[A-Za-z][A-Za-z0-9']{2,}", s):
        out.add(tok.lower())
    for run in re.findall(r"[\u4e00-\u9fff]+", s):
        for i in range(len(run) - 1):
            out.add(run[i : i + 2])
    return out


def detect_persona_drift(
    persona: Dict[str, Any], *, profile: str = "", inside_jokes: Optional[Sequence[str]] = None,
) -> List[str]:
    """C4：检测巩固产物（关系画像/内部梗）是否与人设**硬边界**冲突（防长期跑偏）。

    保守只查高置信冲突：巩固文本命中人设 ``speaking.forbidden_phrases`` 或
    ``boundaries.topics_to_avoid``（这些是明确不该出现的）。返回冲突项清单（空=没漂移）。
    纯函数——供常驻门禁 + 运行期告警共用（不误伤：只查明令禁止项，不做模糊语义判定）。
    """
    if not isinstance(persona, dict):
        return []
    hay = (str(profile or "") + " " + " ".join(_as_list(inside_jokes))).lower()
    if not hay.strip():
        return []
    hits: List[str] = []
    spk = persona.get("speaking") or {}
    for p in _as_list(spk.get("forbidden_phrases") if isinstance(spk, dict) else None):
        if p.lower() in hay:
            hits.append(p)
    bnd = persona.get("boundaries") or {}
    for t in _as_list(bnd.get("topics_to_avoid") if isinstance(bnd, dict) else None):
        if t.lower() in hay:
            hits.append(t)
    return hits


def run_deep_persona_consolidation(
    inbox_store: Any, deep_store: Any, conversation_id: str, *,
    now: Optional[datetime] = None, msg_limit: int = 200,
    persona: Optional[Dict[str, Any]] = None, llm_fn: Optional[Any] = None,
    embedder: Optional[Any] = None,
) -> Dict[str, Any]:
    """IO 编排：从 inbox_store 取信号 → 巩固关系画像 + 内部梗 → 写 deep_store。

    best-effort：任何一步失败都吞掉，返回 {"profile": bool, "jokes": int, "drift": [...]}。
    与既有 store 只读交互（list_messages/get_conv_meta/get_conversation），不改其状态。
    ``persona`` 给出时做 C4 漂移守卫：巩固画像若命中人设硬禁项 → **不写**，记 drift。
    """
    result: Dict[str, Any] = {"profile": False, "jokes": 0, "drift": [], "healed": False,
                              "emb_backfilled": 0}
    cid = str(conversation_id or "").strip()
    if not cid or inbox_store is None or deep_store is None:
        return result
    try:
        msgs = inbox_store.list_messages(cid, limit=msg_limit) or []
    except Exception:
        msgs = []
    inbound = [
        str(m.get("text") or "").strip()
        for m in msgs
        if isinstance(m, dict) and str(m.get("direction") or "") in ("in", "incoming", "inbound")
        and str(m.get("text") or "").strip()
    ]
    # 内部梗
    try:
        jokes = detect_recurring_phrases(inbound, min_count=3, top_k=5)
        if jokes:
            deep_store.add_inside_jokes(cid, jokes)
            result["jokes"] = len(jokes)
    except Exception:
        pass
    # 关系画像
    try:
        meta = {}
        try:
            meta = inbox_store.get_conv_meta(cid) or {}
        except Exception:
            meta = {}
        display_name = ""
        try:
            conv = inbox_store.get_conversation(cid) or {}
            display_name = str(conv.get("display_name") or "").strip()
        except Exception:
            pass
        experiential = []
        try:
            experiential = deep_store.get_experiential(cid) or []
        except Exception:
            pass
        # G2：批量回填缺失的事件向量（off 热路；embedder 就绪才做），补齐历史经历的语义召回能力
        if callable(embedder):
            _backfilled = 0
            for e in experiential:
                try:
                    if e.get("what") and not e.get("emb"):
                        _v = embedder(str(e.get("what")))
                        if _v:
                            deep_store.set_experiential_embedding(cid, str(e["what"]), _v)
                            _backfilled += 1
                except Exception:
                    continue
            if _backfilled:
                result["emb_backfilled"] = _backfilled
        profile = build_profile_from_signals(
            display_name=display_name, inbound_texts=inbound,
            dominant_emotion=str(meta.get("last_emotion") or ""),
            experiential=experiential,
        )
        if profile:
            drift = detect_persona_drift(persona or {}, profile=profile) if persona else []
            if drift:
                result["drift"] = drift  # 漂移：不写新画像
                # D4 自愈：若旧画像也已被污染（命中同样禁项），清掉它，避免脏画像长期驻留；
                # 下一轮从干净信号重建。旧画像干净则保留。
                try:
                    _old = deep_store.get_relationship_profile(cid)
                    if _old and detect_persona_drift(persona or {}, profile=_old):
                        deep_store.set_relationship_profile(cid, "")
                        result["healed"] = True
                except Exception:
                    pass
            else:
                # E2：可选 LLM 精修（过 guard/drift 回落，缺 llm_fn 原样）
                if callable(llm_fn):
                    profile = refine_profile_llm(profile, persona or {}, llm_fn=llm_fn)
                deep_store.set_relationship_profile(cid, profile)
                result["profile"] = True
    except Exception:
        pass
    return result


__all__ = [
    "pick_life_beat", "format_life_context", "life_theme",
    "build_relationship_profile", "format_relationship_profile", "build_callback_opener",
    "format_tastes", "detect_recurring_phrases", "format_inside_jokes",
    "rank_by_affect", "to_experiential_recall",
    "temporal_anchor", "maybe_imperfection_hint",
    "build_deep_persona_block",
    "build_profile_from_signals", "find_resolved_loops", "run_deep_persona_consolidation",
    "select_experiential", "build_life_beat_opener", "detect_persona_drift",
    "life_share_allowed", "refine_profile_llm",
    "life_share_time_ok", "cosine_sim", "make_embedding_sim_fn",
]
