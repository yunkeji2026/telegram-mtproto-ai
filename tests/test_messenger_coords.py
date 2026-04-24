"""messenger_rpa.coords 标定点单测。"""
from __future__ import annotations

from src.integrations.messenger_rpa.coords import (
    BASE_HEIGHT,
    BASE_WIDTH,
    INBOX_SEARCH_BAR,
    inbox_search_tap_candidates,
)


def test_inbox_search_tap_candidates_primary_first() -> None:
    c = inbox_search_tap_candidates(BASE_WIDTH, BASE_HEIGHT)
    assert len(c) >= 3
    assert c[0] == INBOX_SEARCH_BAR.at(BASE_WIDTH, BASE_HEIGHT)
