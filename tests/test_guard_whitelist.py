"""S1-P0A: 测试共享 guard 白名单逻辑。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from src.integrations.shared.guard_whitelist import (
    classify_guard,
    is_inbox_false_positive,
    is_restore_chat_modal,
)


@dataclass
class _MockGuard:
    needs_human: bool = True
    title: str = ""
    type: str = "profile_picker"


# ── 误报场景 ───────────────────────────────────

@pytest.mark.parametrize("title", [
    "Ask Meta AI or search",
    "Meta AI",
    "Facebook Messenger",
    "Messenger",
    "Stories banner",
    "Stories",
    "Stories 行下方紧挨着有矩形会话行，漏了 row_index=0",  # Vision prompt 泄漏
    "",  # 空 title
    "x" * 80,  # 超长 title
    "Yunshan Zan",  # 联系人名（非 modal 关键词）
    "Victor Zan",
    "John 发送了一条消息",
    "Alice sent a photo",
    "Bob sent you a message",
])
def test_inbox_false_positive_recognized(title: str) -> None:
    """已知误报场景应被识别为 false_positive。"""
    g = _MockGuard(title=title)
    assert is_inbox_false_positive(g) is True, f"漏判: {title!r}"


# ── 真实 modal 不该被误判为 false positive ──────

@pytest.mark.parametrize("title", [
    "Choose your Facebook account",
    "Switch account",
    "Log in with Facebook",
    "Sign in to Messenger",
    "Continue as John",
    "选择账号",
    "切换账号",
    "登录 Messenger",
    "アカウントを選択",
    "ログイン",
])
def test_real_modal_not_flagged(title: str) -> None:
    """真实 modal 不应被白名单过滤。"""
    g = _MockGuard(title=title)
    assert is_inbox_false_positive(g) is False, f"误杀真 modal: {title!r}"


# ── needs_human=False 直接返回 False ─────────────

def test_no_needs_human_returns_false() -> None:
    g = _MockGuard(needs_human=False, title="anything")
    assert is_inbox_false_positive(g) is False


# ── restore chat modal 检测 ─────────────────────

@pytest.mark.parametrize("title", [
    "Restore conversations?",
    "Restore chat history",
    "还原聊天记录",
    "还原",
    "履歴を復元",
])
def test_restore_chat_modal_recognized(title: str) -> None:
    g = _MockGuard(needs_human=True, title=title, type="other_modal")
    assert is_restore_chat_modal(g) is True, f"未识别 restore: {title!r}"


def test_restore_modal_classify() -> None:
    g = _MockGuard(needs_human=True, title="还原聊天记录", type="other_modal")
    assert classify_guard(g) == "restore_chat"


def test_false_positive_classify() -> None:
    g = _MockGuard(needs_human=True, title="Yunshan Zan", type="profile_picker")
    assert classify_guard(g) == "false_positive"


def test_real_modal_classify() -> None:
    g = _MockGuard(needs_human=True, title="Choose Facebook account", type="profile_picker")
    assert classify_guard(g) == "real"


# ── 边界：含品牌名但又含真关键词应判真 modal ─────

def test_brand_name_with_real_keyword() -> None:
    """如 'Choose Facebook Messenger account' 含 messenger 又含 choose/account → 真 modal"""
    g = _MockGuard(title="Choose Facebook Messenger account")
    assert is_inbox_false_positive(g) is False
