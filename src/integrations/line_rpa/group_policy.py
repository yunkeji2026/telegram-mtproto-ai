"""群聊回复策略：纯函数层。把 group/mention 判定 + policy 决策从 Runner 剥离。

给出 3 档策略：
    - all          任何消息都回（默认；与 P2-4 之前行为一致）
    - mention_only 仅"被 @ 我"时回（群里常见需求）
    - never        群里完全不回

返回的 `GroupVerdict` 标出：
    - is_group      : 当前是否为群聊
    - mentioned     : 是否检测到 @我
    - should_reply  : 是否应当继续走 AI 回复流程
    - skip_step     : 若不回，对应写入历史的 step 名（便于 Web 展示"为什么没回"）
    - style_hint    : 本条消息最终用的风格提示（可能被 mentioned 提权替换）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional

from src.integrations.line_rpa import ui_hierarchy as ui


@dataclass
class GroupVerdict:
    is_group: bool
    group_debug: str
    mentioned: bool
    mention_debug: str
    should_reply: bool
    skip_step: Optional[str]  # "group_policy_never" / "group_not_mentioned" / None
    style_hint: str


def _normalize_policy(v: Any) -> str:
    s = str(v or "all").strip().lower()
    if s in ("all", "mention_only", "never"):
        return s
    return "all"


def evaluate(
    *,
    xml: bytes,
    peer_text: Optional[str],
    line_pkg: str,
    self_names: List[str],
    group_reply_policy: Any,
    default_style_hint: str,
    mentioned_style_hint: str,
) -> GroupVerdict:
    is_group, group_dbg = ui.detect_group_chat(xml, line_pkg=line_pkg)
    mentioned = False
    mention_dbg = "not_checked"
    if is_group:
        mentioned, mention_dbg = ui.detect_mentioned(
            xml, peer_text=peer_text, self_names=list(self_names or []),
        )

    policy = _normalize_policy(group_reply_policy)
    should = True
    skip: Optional[str] = None
    if is_group:
        if policy == "never":
            should, skip = False, "group_policy_never"
        elif policy == "mention_only" and not mentioned:
            should, skip = False, "group_not_mentioned"

    hint = (default_style_hint or "").strip()
    if is_group and mentioned:
        mh = (mentioned_style_hint or "").strip()
        if mh:
            hint = mh

    return GroupVerdict(
        is_group=is_group,
        group_debug=group_dbg,
        mentioned=mentioned,
        mention_debug=mention_dbg,
        should_reply=should,
        skip_step=skip,
        style_hint=hint,
    )
