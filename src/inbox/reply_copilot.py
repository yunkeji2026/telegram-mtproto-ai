"""P42/P49 — 坐席 AI 副驾：实时打字辅助（Reply Copilot）。

纯规则 + 模板前缀匹配，零 LLM 延迟。
P49：与关系阶段 / 工作链 / @mention / 流失预警深度联动。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.utils.companion_relationship import STAGE_LABEL_ZH

_SOURCE_LABELS = {
    "empathy": "共情",
    "mirror": "承接",
    "stage_default": "阶段语气",
    "callback": "历史回调",
    "template": "话术库",
    "empathy_complete": "共情补全",
    "continuation": "续写",
    "stage_suffix": "语气后缀",
    "workflow_chain": "工作链",
    "mention_help": "同事协助",
    "reunion": "久别重逢",
    "churn_recovery": "流失挽回",
    "stage_advance": "关系进阶",
    "script_topic": "剧本话题",
}

# 阶段语气补全后缀
_STAGE_SUFFIXES = {
    "initial": [
        " 有什么想聊的都可以跟我说。",
        " 慢慢说，不着急。",
    ],
    "warming": [
        " 我很乐意听你说。",
        " 你继续说，我在。",
    ],
    "intimate": [
        " 跟你聊天总是很开心。",
        " 你说什么我都想听。",
    ],
    "steady": [
        " 不管什么时候，都可以找我。",
        " 我一直在这里。",
    ],
}

_EMPATHY_STARTERS = [
    "我能理解你的感受，",
    "听起来你最近挺不容易的，",
    "谢谢你愿意跟我分享这些，",
]

_CONTINUATION_STARTERS = [
    "嗯，",
    "是的，",
    "我明白，",
    "原来如此，",
]

_CHURN_OPENERS = [
    "好久没见你上线了，最近还好吗？不用有压力，想到什么就说什么。",
    "这段时间没聊，我一直在想你是不是太忙了——有空的时候跟我说说话好吗？",
]

_REUNION_OPENERS = [
    "好久不见！最近过得怎么样？不用刻意找话题，随便聊聊就好。",
    "终于又见到你了，这段时间有什么新鲜事想分享吗？",
]


class ReplyCopilot:
    """P42/P49：打字辅助建议生成器。"""

    def suggest(
        self,
        *,
        partial_text: str = "",
        last_customer_msg: str = "",
        stage: str = "initial",
        recent_messages: Optional[List[Dict[str, Any]]] = None,
        templates: Optional[List[Dict[str, Any]]] = None,
        context: Optional[Dict[str, Any]] = None,
        limit: int = 3,
    ) -> Dict[str, Any]:
        """生成回复补全建议。

        Returns:
            {suggestions: [{text, source, source_label, rationale, confidence}], context_header}
        """
        partial = (partial_text or "").strip()
        ctx = context or {}
        stage = str(ctx.get("stage") or stage or "initial")
        if stage not in _STAGE_SUFFIXES:
            stage = "initial"
        recent = recent_messages or []
        tpls = templates or []
        results: List[Dict[str, Any]] = []

        customer_lc = (last_customer_msg or "").lower()
        is_negative = any(
            kw in customer_lc
            for kw in ["难过", "伤心", "孤独", "烦", "累", "sad", "lonely", "tired"]
        )

        header = self._context_header(ctx, stage)

        if not partial:
            results.extend(self._context_suggestions(ctx, stage, is_negative, limit))
            results.extend(self._full_suggestions(
                last_customer_msg, stage, is_negative, recent, limit,
            ))
        else:
            wf = str(ctx.get("workflow_text") or "").strip()
            if wf and wf.lower().startswith(partial.lower()) and wf != partial:
                results.append(self._item(
                    wf, "workflow_chain",
                    f"工作链「{ctx.get('workflow_chain_name') or '步骤'}」建议话术",
                    0.98,
                ))
            results.extend(self._completions(
                partial, stage, is_negative, tpls, limit,
            ))

        unique = self._dedupe(results)[:limit]
        return {"suggestions": unique, "context_header": header}

    def _context_header(self, ctx: Dict[str, Any], stage: str) -> str:
        parts: List[str] = []
        trigger = str(ctx.get("trigger") or "")
        label = str(ctx.get("stage_label") or STAGE_LABEL_ZH.get(stage, stage))
        if trigger == "workflow_step":
            parts.append(f"⚡ 工作链步骤 {int(ctx.get('workflow_step') or 0) + 1}")
        elif trigger == "mention":
            who = ctx.get("mention_from") or "同事"
            parts.append(f"👥 {who} 请求协助")
        elif trigger == "stage_advance":
            nxt = ctx.get("next_stage_label") or ""
            parts.append(f"💞 关系进阶 → {nxt or label}")
        elif trigger == "reunion":
            parts.append("🌸 久别重逢策略")
        elif trigger == "churn":
            parts.append("🛡 高流失挽回")
        elif ctx.get("churn_level") == "high":
            parts.append("⚠ 高流失关注")
        elif label:
            parts.append(f"阶段：{label}")
        return " · ".join(parts)

    def _context_suggestions(
        self,
        ctx: Dict[str, Any],
        stage: str,
        is_negative: bool,
        limit: int,
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        trigger = str(ctx.get("trigger") or "")

        wf = str(ctx.get("workflow_text") or "").strip()
        if wf:
            out.append(self._item(
                wf,
                "workflow_chain",
                f"工作链「{ctx.get('workflow_chain_name') or '步骤'}」建议话术",
                0.98,
            ))

        mention_body = str(ctx.get("mention_note") or "").strip()
        if mention_body or trigger == "mention":
            from_name = ctx.get("mention_from") or "同事"
            out.append(self._item(
                f"好的，我看到你的留言了。关于同事提到的协助请求，我先跟你聊聊近况好吗？",
                "mention_help",
                f"回应 {from_name} 的 @协助请求",
                0.92,
            ))
            if mention_body:
                snippet = mention_body[:40]
                out.append(self._item(
                    f"关于注解里说的「{snippet}{'…' if len(mention_body) > 40 else ''}」，我来跟进一下。",
                    "mention_help",
                    "承接内部协作注解",
                    0.9,
                ))

        if ctx.get("reunion") or trigger == "reunion":
            for i, text in enumerate(_REUNION_OPENERS[:2]):
                out.append(self._item(text, "reunion", "久别重逢 · 自然问候", 0.93 - i * 0.03))

        churn = str(ctx.get("churn_level") or "")
        if churn == "high" or trigger == "churn":
            for i, text in enumerate(_CHURN_OPENERS[:2]):
                out.append(self._item(text, "churn_recovery", "高流失客户温和挽回", 0.91 - i * 0.02))

        if trigger == "stage_advance" or ctx.get("pending_advancement"):
            nxt = str(ctx.get("next_stage_label") or ctx.get("pending_stage_label") or "")
            if nxt:
                out.append(self._item(
                    f"感觉我们越来越熟悉了，想跟你聊点更深入的话题——你觉得怎么样？",
                    "stage_advance",
                    f"关系进阶至「{nxt}」的过渡话术",
                    0.88,
                ))

        for topic in (ctx.get("script_topics") or [])[:2]:
            opener = str(topic.get("opener") or "").strip()
            if opener:
                out.append(self._item(
                    opener,
                    "script_topic",
                    f"剧本：{topic.get('title') or '话题'}",
                    0.86,
                ))

        if is_negative and not any(r.get("source") == "empathy" for r in out):
            out.append(self._item(
                _EMPATHY_STARTERS[0] + "能多跟我说说吗？",
                "empathy",
                "客户情绪偏负面，优先共情",
                0.9,
            ))

        return out[: max(limit, 4)]

    def _full_suggestions(
        self,
        customer_msg: str,
        stage: str,
        is_negative: bool,
        recent: List[Dict[str, Any]],
        limit: int,
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        label = STAGE_LABEL_ZH.get(stage, stage)

        if is_negative:
            for starter in _EMPATHY_STARTERS[:2]:
                out.append(self._item(
                    starter + "能多跟我说说吗？",
                    "empathy",
                    "检测到客户情绪偏负面，优先共情",
                    0.9,
                ))

        if customer_msg:
            snippet = customer_msg[:20].strip()
            if snippet:
                out.append(self._item(
                    f"关于你说的「{snippet}{'…' if len(customer_msg) > 20 else ''}」，我想多听听你的想法。",
                    "mirror",
                    "承接客户原话，表达倾听意愿",
                    0.85,
                ))

        suffixes = _STAGE_SUFFIXES.get(stage, _STAGE_SUFFIXES["initial"])
        for i, suf in enumerate(suffixes[:2]):
            starter = "你好呀，" if stage == "initial" else ""
            out.append(self._item(
                starter + f"（{label}阶段）" + suf.strip(),
                "stage_default",
                f"适合{label}阶段的默认语气",
                0.7 - i * 0.05,
            ))

        if len(recent) >= 10:
            out.append(self._item(
                "我们之前聊的那个话题，后来你怎么想的？我一直记挂着。",
                "callback",
                "多轮对话，引用历史增强连接感",
                0.75,
            ))

        return out

    def _completions(
        self,
        partial: str,
        stage: str,
        is_negative: bool,
        templates: List[Dict[str, Any]],
        limit: int,
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        partial_lc = partial.lower()

        for tpl in templates:
            body = str(tpl.get("body") or tpl.get("content") or tpl.get("text") or "").strip()
            if not body:
                continue
            if body.lower().startswith(partial_lc) and body != partial:
                out.append(self._item(
                    body,
                    "template",
                    f"模板：{tpl.get('title') or tpl.get('scene') or '话术库'}",
                    0.95,
                ))

        if is_negative and partial.startswith("我"):
            for starter in _EMPATHY_STARTERS:
                candidate = starter + partial[1:] if len(partial) > 1 else starter.strip("，")
                if candidate.lower().startswith(partial_lc):
                    out.append(self._item(
                        candidate, "empathy_complete", "共情型补全", 0.88,
                    ))

        if len(out) < limit:
            for starter in _CONTINUATION_STARTERS:
                candidate = starter + partial
                if candidate != partial:
                    out.append(self._item(
                        candidate, "continuation", "自然接话续写", 0.6,
                    ))

        if len(out) < limit and not partial.endswith(("。", "！", "?", "？")):
            for suf in _STAGE_SUFFIXES.get(stage, [])[:1]:
                out.append(self._item(
                    partial.rstrip("，,.") + suf,
                    "stage_suffix",
                    "追加阶段语气",
                    0.65,
                ))

        return out

    def _item(
        self, text: str, source: str, rationale: str, confidence: float,
    ) -> Dict[str, Any]:
        return {
            "text": text,
            "source": source,
            "source_label": _SOURCE_LABELS.get(source, source),
            "rationale": rationale,
            "confidence": confidence,
        }

    def _dedupe(self, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen: set = set()
        unique: List[Dict[str, Any]] = []
        for r in results:
            t = r.get("text", "").strip()
            if t and t not in seen:
                seen.add(t)
                unique.append(r)
        return unique
