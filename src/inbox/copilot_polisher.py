"""P52 — Copilot LLM 润色层（规则建议 → 自然口语化）。

默认关闭（config ai.copilot_polish.enabled: false）。
失败/超时回退规则原文，不阻断坐席。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional

from src.utils.companion_relationship import STAGE_LABEL_ZH

logger = logging.getLogger(__name__)

_SKIP_SOURCES = frozenset({"workflow_chain", "template"})

_TRIGGER_LABELS = {
    "open": "打开会话",
    "stage_advance": "关系进阶",
    "reunion": "久别重逢",
    "churn": "流失挽回",
    "mention": "同事协助",
    "workflow_step": "工作链步骤",
}

_STAGE_TONE = {
    "initial": "礼貌、轻松，不要太熟络",
    "warming": "温暖、愿意倾听，适度关心",
    "intimate": "亲近、自然，像老朋友聊天",
    "steady": "稳定陪伴，踏实可靠",
}

_DEFAULT_CFG: Dict[str, Any] = {
    "enabled": False,
    "max_suggestions": 2,
    "timeout_sec": 8,
    "temperature": 0.6,
    "max_tokens": 320,
    "model": "",
}


def get_polish_config(config_manager: Any) -> Dict[str, Any]:
    """读取 ai.copilot_polish 配置（缺省全关）。"""
    cfg = dict(_DEFAULT_CFG)
    if config_manager is None:
        return cfg
    try:
        ai = (getattr(config_manager, "config", None) or {}).get("ai") or {}
        raw = ai.get("copilot_polish") or {}
        if isinstance(raw, dict):
            cfg.update({k: raw[k] for k in cfg if k in raw})
            if "enabled" in raw:
                cfg["enabled"] = bool(raw["enabled"])
    except Exception:
        pass
    return cfg


def should_polish(
    *,
    polish_requested: bool,
    partial_text: str,
    cfg: Dict[str, Any],
) -> bool:
    """是否执行润色：配置开启 + 显式/预填请求 + 非打字补全。"""
    if not cfg.get("enabled"):
        return False
    if not polish_requested:
        return False
    # 打字补全默认不润色（延迟敏感），除非调用方显式 polish 且 partial 为空
    if (partial_text or "").strip():
        return False
    return True


def _build_prompt(
    candidates: List[Dict[str, Any]],
    *,
    context: Dict[str, Any],
    last_customer_msg: str,
) -> str:
    stage = str(context.get("stage") or "initial")
    stage_label = str(
        context.get("stage_label") or STAGE_LABEL_ZH.get(stage, stage)
    )
    trigger = str(context.get("trigger") or "open")
    tone = _STAGE_TONE.get(stage, _STAGE_TONE["initial"])
    lines = [
        "你是情感陪伴坐席的回复润色助手。将草稿润色得更自然口语化，保持原意与策略不变。",
        f"关系阶段：{stage_label}（语气：{tone}）",
        f"场景：{_TRIGGER_LABELS.get(trigger, trigger)}",
    ]
    if context.get("recent_downgrade"):
        lines.append("注意：该客户近期被手动降级，语气宜更克制、尊重边界，勿过于热情。")
    if context.get("reunion") or trigger == "reunion":
        lines.append("久别重逢：先自然问候，勿直接接旧梗或过于亲密。")
    if str(context.get("churn_level") or "") == "high" or trigger == "churn":
        lines.append("高流失风险：温和挽回，不给压力。")
    if last_customer_msg:
        snippet = last_customer_msg[:200]
        lines.append(f"客户最近消息：{snippet}")
    lines.append("")
    lines.append("草稿（JSON 数组，保留 index）：")
    payload = [
        {
            "index": i,
            "text": str(c.get("text") or ""),
            "intent": str(c.get("rationale") or c.get("source_label") or ""),
        }
        for i, c in enumerate(candidates)
    ]
    lines.append(json.dumps(payload, ensure_ascii=False))
    lines.append("")
    lines.append(
        '只输出 JSON 数组：[{"index":0,"text":"润色后正文"},...]，'
        "不要 markdown、不要解释。每条不超过 120 字。"
    )
    return "\n".join(lines)


def _parse_polish_response(raw: str, n: int) -> List[Dict[str, str]]:
    """解析 LLM 输出，失败返回空列表。"""
    text = (raw or "").strip()
    if not text:
        return []
    # 去掉 markdown 代码块
    if "```" in text:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if m:
            text = m.group(1).strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "text" in data:
            data = [data]
        if not isinstance(data, list):
            return []
        out: List[Dict[str, str]] = []
        for item in data[:n]:
            if not isinstance(item, dict):
                continue
            idx = int(item.get("index", len(out)))
            body = str(item.get("text") or "").strip()
            if body:
                out.append({"index": str(idx), "text": body})
        return out
    except json.JSONDecodeError:
        pass
    # 单行回退：若只润色一条，整段当作结果
    if n == 1 and len(text) <= 200 and not text.startswith("["):
        return [{"index": "0", "text": text}]
    return []


def _select_candidates(
    suggestions: List[Dict[str, Any]],
    max_n: int,
) -> List[tuple]:
    """返回 [(原索引, suggestion), ...] 待润色条目。"""
    picked: List[tuple] = []
    for i, s in enumerate(suggestions):
        if len(picked) >= max_n:
            break
        src = str(s.get("source") or "")
        if src in _SKIP_SOURCES:
            continue
        text = str(s.get("text") or "").strip()
        if not text or len(text) > 300:
            continue
        picked.append((i, s))
    return picked


def apply_polish_results(
    suggestions: List[Dict[str, Any]],
    polished: List[Dict[str, str]],
    indices: List[int],
) -> List[Dict[str, Any]]:
    """将润色结果写回建议列表。"""
    if not polished:
        return suggestions
    out = [dict(s) for s in suggestions]
    by_idx = {}
    for j, item in enumerate(polished):
        try:
            key = int(item.get("index", j))
        except (TypeError, ValueError):
            key = j
        by_idx[key] = item.get("text", "")
    for j, orig_i in enumerate(indices):
        new_text = by_idx.get(j) or by_idx.get(orig_i) or ""
        if not new_text:
            continue
        orig = out[orig_i]
        orig["original_text"] = orig.get("text", "")
        orig["text"] = new_text
        orig["polished"] = True
        orig["source"] = "copilot_polish"
        orig["source_label"] = "AI 润色"
        orig["rationale"] = (orig.get("rationale") or "") + "（LLM 润色）"
    return out


async def polish_suggestions(
    ai_client: Any,
    suggestions: List[Dict[str, Any]],
    *,
    context: Dict[str, Any],
    last_customer_msg: str = "",
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """润色建议列表；返回 {suggestions, polished, polish_error}。"""
    cfg = cfg or _DEFAULT_CFG
    max_n = max(1, min(3, int(cfg.get("max_suggestions") or 2)))
    picked = _select_candidates(suggestions, max_n)
    if not picked or ai_client is None:
        return {
            "suggestions": suggestions,
            "polished": False,
            "polish_skipped": "no_candidates" if suggestions else "empty",
        }

    candidates = [s for _, s in picked]
    indices = [i for i, _ in picked]
    prompt = _build_prompt(candidates, context=context, last_customer_msg=last_customer_msg)

    overrides: Dict[str, Any] = {
        "temperature": float(cfg.get("temperature") or 0.6),
        "max_tokens": int(cfg.get("max_tokens") or 320),
        "context_rounds": 0,
    }
    model = str(cfg.get("model") or "").strip()
    if model:
        overrides["model"] = model

    timeout = float(cfg.get("timeout_sec") or 8)
    try:
        raw = await asyncio.wait_for(
            ai_client.chat(prompt, strategy_overrides=overrides),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.info("copilot_polish timeout after %.1fs", timeout)
        return {
            "suggestions": suggestions,
            "polished": False,
            "polish_error": "timeout",
        }
    except Exception as exc:
        logger.warning("copilot_polish failed: %s", exc)
        return {
            "suggestions": suggestions,
            "polished": False,
            "polish_error": str(exc)[:120],
        }

    parsed = _parse_polish_response(str(raw or ""), len(picked))
    if not parsed:
        return {
            "suggestions": suggestions,
            "polished": False,
            "polish_error": "parse_failed",
        }

    merged = apply_polish_results(suggestions, parsed, indices)
    return {"suggestions": merged, "polished": True, "polish_count": len(parsed)}
