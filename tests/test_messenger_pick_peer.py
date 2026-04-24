"""pick_unread_row_for_peer_name：找好友 / 预览弱匹配。"""

from __future__ import annotations

from src.integrations.messenger_rpa.inbox_scanner import UnreadChat
from src.integrations.messenger_rpa.runner import pick_unread_row_for_peer_name


def _row(name: str, preview: str, ri: int = 0) -> UnreadChat:
    return UnreadChat(
        name=name,
        preview=preview,
        time="",
        row_index=ri,
        y_percent=0.0,
        quality_hint="friend",
        score=10.0,
    )


def test_pick_by_name_exact() -> None:
    unread = [_row("Alice", "hi"), _row("Bob", "yo")]
    r = pick_unread_row_for_peer_name(unread, "Alice", [])
    assert r is not None and r.name == "Alice"


def test_pick_prefers_full_name_over_short_prefix() -> None:
    """列表里同时有 Victor 与 Victor Zan 时，目标 Victor Zan 不得命中 Victor。"""
    unread = [_row("Victor", "other"), _row("Victor Zan", "hello")]
    r = pick_unread_row_for_peer_name(unread, "Victor Zan", [], min_preview_substr_len=0)
    assert r is not None and r.name == "Victor Zan"


def test_pick_by_preview_unique() -> None:
    unread = [
        _row("Unknown", "Victor Zan: hello there"),
        _row("Other", "spam text"),
    ]
    hints: list[str] = []
    r = pick_unread_row_for_peer_name(
        unread, "Victor Zan", [], min_preview_substr_len=4, hint_out=hints,
    )
    assert r is not None
    assert "Victor Zan" in r.preview
    assert any("preview" in h for h in hints)


def test_pick_preview_ambiguous_returns_none() -> None:
    unread = [
        _row("Alex", "say Victor Zan to me"),
        _row("Bianca", "Victor Zan was here"),
    ]
    r = pick_unread_row_for_peer_name(
        unread, "Victor Zan", [], min_preview_substr_len=4,
    )
    assert r is None


def test_pick_from_ranking_preview_unique() -> None:
    unread: list[UnreadChat] = []
    rk = [
        {
            "name": "X",
            "preview": "nothing",
            "hint": "unsure",
            "score": 1.0,
            "row_index": 2,
        },
        {
            "name": "Y",
            "preview": "Victor Zan replied OK",
            "hint": "friend",
            "score": 2.0,
            "row_index": 4,
        },
    ]
    hints: list[str] = []
    r = pick_unread_row_for_peer_name(
        unread, "Victor Zan", rk, min_preview_substr_len=4, hint_out=hints,
    )
    assert r is not None and r.row_index == 4
    assert any("ranking_preview" in h for h in hints)
