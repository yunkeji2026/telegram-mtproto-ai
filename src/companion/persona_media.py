"""每人设相册「按关键词触发挑媒体」匹配器（纯函数，可单测/离线）。

给「运营配触发词 → 对话命中就发对应图/视频」提供选择逻辑。**只做挑选，不碰 IO**
（取行由 ``persona_media_store`` 负责；发送由 image_autosend / skill_manager 负责）。

匹配语义（简单、可解释、零误伤）：
- 条目带 ``triggers``（关键词列表）：客户文本包含任一关键词 → **精确命中**（specific）。
- 条目 ``triggers`` 为空 = **通用相册池**：仅当调用方判定这是一次「泛化要照片/自拍」请求
  （``generic_ok=True``）时才作候选——对应老「随机挑一张自拍」的行为，向后兼容。
- **优先级**：只要有精确命中就用精确命中池，否则才回落通用池；都空 → None（交生成/文字兜底）。
- 多候选：``weight`` 加权随机 + **尽量避开上一条**（``avoid_id``）防连发重复（新鲜感）。
- 可选按 ``media_types`` 过滤（只要图/只要视频）与 ``bond_level`` 关系闸门（默认不设=不限）。
"""
from __future__ import annotations

import random
from typing import Any, Dict, List, Optional, Sequence, Tuple

POOL_KEYWORD = "keyword"
POOL_GENERIC = "generic"
POOL_NONE = "none"


def normalize_text(text: Any) -> str:
    return str(text or "").strip().lower()


def _triggers_hit(triggers: Sequence[Any], text_norm: str) -> bool:
    for t in triggers or []:
        ts = str(t or "").strip().lower()
        if ts and ts in text_norm:
            return True
    return False


def _partition(
    rows: Sequence[Dict[str, Any]], text: str, *,
    media_types: Optional[Sequence[str]] = None,
    bond_level: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """把候选行分成 (关键词命中, 通用池)，同时过 enabled / 类型 / 关系闸门。"""
    tn = normalize_text(text)
    mt_set = {str(m).strip().lower() for m in media_types} if media_types else None
    keyword: List[Dict[str, Any]] = []
    generic: List[Dict[str, Any]] = []
    for r in rows or []:
        if not r.get("enabled", True):
            continue
        if mt_set and str(r.get("media_type") or "").lower() not in mt_set:
            continue
        if bond_level is not None and int(r.get("min_bond_level") or 0) > int(bond_level):
            continue
        trg = r.get("triggers") or []
        if trg:
            if _triggers_hit(trg, tn):
                keyword.append(r)
        else:
            generic.append(r)
    return keyword, generic


def _weighted_choice(
    rows: Sequence[Dict[str, Any]], rng: Optional[random.Random] = None,
) -> Dict[str, Any]:
    r = rng or random
    weights = [max(1, int(x.get("weight") or 1)) for x in rows]
    return r.choices(list(rows), weights=weights, k=1)[0]


def select_media(
    rows: Sequence[Dict[str, Any]], text: str, *,
    generic_ok: bool = False,
    media_types: Optional[Sequence[str]] = None,
    avoid_id: str = "",
    bond_level: Optional[int] = None,
    rng: Optional[random.Random] = None,
) -> Optional[Dict[str, Any]]:
    """从候选行里挑一个媒体条目；挑不到返回 None。纯函数（rng 可注入以确定性测试）。"""
    keyword, generic = _partition(
        rows, text, media_types=media_types, bond_level=bond_level)
    pool = keyword if keyword else (generic if generic_ok else [])
    if not pool:
        return None
    if avoid_id:
        alt = [r for r in pool if str(r.get("id")) != str(avoid_id)]
        if alt:
            pool = alt
    return _weighted_choice(pool, rng)


def explain_match(
    rows: Sequence[Dict[str, Any]], text: str, *,
    generic_ok: bool = True,
    media_types: Optional[Sequence[str]] = None,
    bond_level: Optional[int] = None,
) -> Dict[str, Any]:
    """「试触发」用：返回这句话会命中哪个池 + 全部候选（不做加权随机，列全供预览）。"""
    keyword, generic = _partition(
        rows, text, media_types=media_types, bond_level=bond_level)
    if keyword:
        pool, cands = POOL_KEYWORD, keyword
    elif generic_ok and generic:
        pool, cands = POOL_GENERIC, generic
    else:
        pool, cands = POOL_NONE, []
    return {
        "pool": pool,
        "candidates": [
            {"id": c.get("id"), "media_type": c.get("media_type"),
             "url": c.get("url"), "caption": c.get("caption"),
             "triggers": c.get("triggers") or [], "weight": c.get("weight")}
            for c in cands
        ],
        "keyword_count": len(keyword),
        "generic_count": len(generic),
    }


def pick_media(
    store: Any, persona_id: str, text: str, *,
    generic_ok: bool = False,
    media_types: Optional[Sequence[str]] = None,
    avoid_id: str = "",
    bond_level: Optional[int] = None,
    rng: Optional[random.Random] = None,
) -> Optional[Dict[str, Any]]:
    """store 便捷封装：取该人设 enabled 行 → select_media。store/persona 缺失 → None。"""
    if store is None or not persona_id:
        return None
    try:
        rows = store.list(str(persona_id), enabled_only=True)
    except Exception:
        return None
    return select_media(
        rows, text, generic_ok=generic_ok, media_types=media_types,
        avoid_id=avoid_id, bond_level=bond_level, rng=rng)


def caption_for(row: Optional[Dict[str, Any]], lang: str = "", *, fallback: str = "") -> str:
    """取条目配文：优先 ``caption_i18n[lang]`` → ``caption`` → fallback。"""
    row = row or {}
    ci = row.get("caption_i18n") or {}
    if lang and isinstance(ci, dict):
        c = str(ci.get(lang) or "").strip()
        if c:
            return c
    return str(row.get("caption") or "").strip() or fallback


__all__ = [
    "POOL_KEYWORD", "POOL_GENERIC", "POOL_NONE",
    "normalize_text", "select_media", "explain_match", "pick_media", "caption_for",
]
