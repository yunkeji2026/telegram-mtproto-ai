"""能力档 × 决策信号 联动建议 + 配置一致性体检（纯函数，闭环纠偏）。

前几增量分别给了：能力档位（off/dry_run/active/blocked）、真实运营信号（healthy/caution/…）。
本层把两者**对齐成一行可执行建议**，并查配置自洽性，收口「看→校→开→观测→**纠偏**」：

  - dry_run + 信号 healthy   → 建议「关 dry_run 转真发」（带一键 target）
  - active(真发) + 信号 failing → 建议「降档：转 dry_run / 关闭」（带一键 target）
  - blocked                  → 建议「修前置」（子系统未挂 / 父开关关）
  - 安全栈 off               → 建议「开启」
  + 一致性体检：真发开但 worker 关 / 无 auto_ai / send-gate 裸奔 / 语音真发但文本主开关关。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# 能力 → 适用的决策信号 key（readiness_signals 产出的 key）
_SIGNAL_FOR = {
    "l2_autosend_deliver": "l2_autosend_deliver",
    "proactive_topic": "proactive_topic",
    "proactive_care": "proactive_topic",
}

# 建议动作优先级（越小越先处理）
_ACTION_RANK = {"fix": 0, "downgrade": 1, "enable": 2, "advance": 3,
                "watch": 4, "hold": 5}


def _rec(cap: Dict[str, Any], action: str, reason: str,
         target: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out = {"key": cap["key"], "label": cap["label"], "tier": cap["tier"],
           "action": action, "reason": reason}
    if target is not None:
        target = {"key": cap["key"], **target}
        out["target"] = target
    return out


def build_recommendations(
    caps: List[Dict[str, Any]], signals_by_key: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """逐能力把「当前档 × 信号」翻成一条可执行建议（healthy-active 等无需动作的略过）。"""
    recs: List[Dict[str, Any]] = []
    for cap in caps:
        sig = signals_by_key.get(_SIGNAL_FOR.get(cap["key"], ""))
        verdict = sig.get("verdict") if isinstance(sig, dict) else None
        advice = sig.get("advice") if isinstance(sig, dict) else ""
        stage = cap["stage"]

        if stage == "blocked":
            recs.append(_rec(cap, "fix", cap.get("recommended") or "修复前置条件"))
        elif stage == "dry_run":
            if verdict == "healthy":
                recs.append(_rec(cap, "advance", advice or "灰度信号良好，建议转真发",
                                 {"field": "dry_run", "value": False}))
            else:
                recs.append(_rec(cap, "hold", advice or "继续灰度采样"))
        elif stage == "active":
            if verdict == "failing":
                target = ({"field": "dry_run", "value": True}
                          if cap.get("dry_run_supported")
                          else {"field": "enabled", "value": False})
                recs.append(_rec(cap, "downgrade", advice or "信号异常，建议降档", target))
            elif verdict == "caution":
                recs.append(_rec(cap, "watch", advice or "信号待改进，留意"))
            # healthy / 无信号的 active → 无需动作
        elif stage == "off":
            if cap["kind"] == "safeguard":
                recs.append(_rec(cap, "enable", "安全栈建议开启（关着才危险）",
                                 {"field": "enabled", "value": True}))
            # feature off：运营自主选择，不打扰
    recs.sort(key=lambda r: _ACTION_RANK.get(r["action"], 9))
    return recs


def consistency_issues(
    caps: List[Dict[str, Any]], *, auto_ai: Optional[int] = None,
    embed_ready: Optional[bool] = None,
) -> List[Dict[str, Any]]:
    """配置自洽体检：开关之间互相矛盾 / 前置缺失。

    ``embed_ready``（可选）：嵌入源是否配齐（由 ``embedding_readiness`` 算出）。传 False 且
    记忆向量召回已开 → 报 error（**静默退化关键词**，是「陪护开起来」最隐蔽的坑）。
    """
    by = {c["key"]: c for c in caps}

    def en(key: str) -> bool:
        return bool(by.get(key, {}).get("enabled"))

    issues: List[Dict[str, Any]] = []
    if embed_ready is False and en("memory_vector_recall"):
        issues.append({"severity": "error", "keys": ["memory_vector_recall"],
                       "message": "记忆向量召回已开但未配嵌入源 → embed() 返回空，召回静默退化为"
                                  "纯关键词（看似开了实则没开）。配 ai.embedding_base_url/model "
                                  "或关掉该能力（见 docs/COMPANION_TURN_ON.md）"})
    if en("l2_autosend_deliver"):
        if not en("l2_autosend_worker"):
            issues.append({"severity": "error", "keys": ["l2_autosend_deliver", "l2_autosend_worker"],
                           "message": "真发已开但 L2 worker 未开 → 草稿无人处置，发不出"})
        if not en("companion_send_gate"):
            issues.append({"severity": "warn", "keys": ["l2_autosend_deliver", "companion_send_gate"],
                           "message": "真发已开但出站安全闸未开 → 内容/频率裸奔，建议同开"})
        if auto_ai is not None and auto_ai <= 0:
            issues.append({"severity": "error", "keys": ["l2_autosend_deliver"],
                           "message": "真发已开但无 auto_ai 会话 → 不会对任何人真发（需设会话为全自动）"})
        if "outbound_autosend_translate" in by and not en("outbound_autosend_translate"):
            issues.append({"severity": "warn",
                           "keys": ["l2_autosend_deliver", "outbound_autosend_translate"],
                           "message": "真发已开但出站自动翻译未开 → 外语客户会收到中文原文，"
                                      "建议同开（补「全自动聊天翻译」闭环）"})
    if en("voice_autosend") and not en("l2_autosend_deliver"):
        issues.append({"severity": "warn", "keys": ["voice_autosend", "l2_autosend_deliver"],
                       "message": "语音真发已开但文本真发主开关未开 → 语音多半发不出"})
    for c in caps:
        if c["stage"] == "blocked":
            issues.append({"severity": "warn", "keys": [c["key"]],
                           "message": f"「{c['label']}」开关已开但未生效：{c.get('recommended') or '前置缺失'}"})
    return issues


def build_advice(
    status: Dict[str, Any], signals: Dict[str, Any], *, auto_ai: Optional[int] = None,
    embed_ready: Optional[bool] = None,
) -> Dict[str, Any]:
    """合并：能力档 × 信号 → 建议 + 一致性体检 + 摘要。"""
    caps = (status or {}).get("capabilities", []) or []
    sig_list = (signals or {}).get("signals", []) or []
    signals_by_key = {s.get("key"): s for s in sig_list if isinstance(s, dict)}

    recs = build_recommendations(caps, signals_by_key)
    issues = consistency_issues(caps, auto_ai=auto_ai, embed_ready=embed_ready)
    return {
        "recommendations": recs,
        "consistency": issues,
        "summary": {
            "action_count": len(recs),
            "advance": sum(1 for r in recs if r["action"] == "advance"),
            "downgrade": sum(1 for r in recs if r["action"] == "downgrade"),
            "fix": sum(1 for r in recs if r["action"] == "fix"),
            "errors": sum(1 for i in issues if i["severity"] == "error"),
            "warnings": sum(1 for i in issues if i["severity"] == "warn"),
        },
    }


__all__ = [
    "build_recommendations", "consistency_issues", "build_advice",
]
