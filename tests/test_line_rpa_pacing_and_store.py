"""最小测试：human_pacing 拆分 & state_store 持久化。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.integrations.line_rpa.human_pacing import (
    PacingConfig,
    jitter_ms,
    split_message,
    typing_duration_sec,
)
from src.integrations.line_rpa.state_store import (
    LineRpaStateStore,
    migrate_from_legacy_json,
)


# ───── human_pacing ─────

def test_pacing_from_dict_defaults():
    c = PacingConfig.from_dict(None)
    assert c.enabled is True
    assert c.read_pause_ms_lo <= c.read_pause_ms_hi
    assert c.split_max_parts >= 1
    assert c.split_mode in {"none", "sentence", "length"}


def test_pacing_from_dict_pair_swap():
    c = PacingConfig.from_dict({"read_pause_ms": [2000, 800]})
    assert c.read_pause_ms_lo == 800
    assert c.read_pause_ms_hi == 2000


def test_pacing_from_dict_single_number():
    c = PacingConfig.from_dict({"inter_msg_ms": 1000})
    assert c.inter_msg_ms_lo == 1000
    assert c.inter_msg_ms_hi == 1000


def test_jitter_ms_range():
    for _ in range(20):
        v = jitter_ms(100, 300)
        assert 0.1 <= v <= 0.3 + 1e-6


def test_split_message_none_mode():
    cfg = PacingConfig.from_dict({"split_mode": "none"})
    parts = split_message("你好。今天天气不错。晚点再聊。", cfg)
    assert parts == ["你好。今天天气不错。晚点再聊。"]


def test_split_message_sentence_mode():
    cfg = PacingConfig.from_dict(
        {"split_mode": "sentence", "split_max_chars": 10, "split_max_parts": 3}
    )
    parts = split_message("你好。今天天气不错。晚点再聊。", cfg)
    assert 1 <= len(parts) <= 3
    assert "".join(parts).replace(" ", "") == "你好。今天天气不错。晚点再聊。"


def test_split_message_length_mode_respects_max_parts():
    # split_max_chars 最低 20（PacingConfig 做 clamp），所以用 60 字符确保能拆
    cfg = PacingConfig.from_dict(
        {"split_mode": "length", "split_max_chars": 20, "split_max_parts": 2}
    )
    long_text = "a" * 60
    parts = split_message(long_text, cfg)
    assert len(parts) == 2
    assert "".join(parts) == long_text


def test_split_message_empty_returns_empty():
    cfg = PacingConfig.from_dict({})
    assert split_message("", cfg) == []
    assert split_message("   ", cfg) == []


def test_typing_duration_is_positive():
    cfg = PacingConfig.from_dict({"per_char_ms": [40, 80]})
    assert typing_duration_sec("", cfg) == 0.0
    assert typing_duration_sec("hello", cfg) >= 0.2


# ───── state_store ─────

@pytest.fixture()
def store(tmp_path: Path) -> LineRpaStateStore:
    s = LineRpaStateStore(tmp_path / "line_rpa_state.db", max_runs_kept=50)
    yield s
    s.close()


def test_chat_state_roundtrip(store: LineRpaStateStore):
    assert store.get_chat_state("ck") == {}
    store.update_chat_state(
        "ck",
        last_peer_text="你好",
        last_reply="你好呀",
        last_screen_sha256="abc" * 10,
    )
    row = store.get_chat_state("ck")
    assert row["last_peer_text"] == "你好"
    assert row["last_reply"] == "你好呀"
    assert row["last_screen_sha256"].startswith("abc")

    # 更新：只改 last_reply
    store.update_chat_state("ck", last_reply="更新")
    row2 = store.get_chat_state("ck")
    assert row2["last_peer_text"] == "你好"
    assert row2["last_reply"] == "更新"


def test_list_chats_order(store: LineRpaStateStore):
    store.update_chat_state("a", last_peer_text="x")
    store.update_chat_state("b", last_peer_text="y")
    store.update_chat_state("a", last_peer_text="x2")  # 刷新 updated_at
    chats = store.list_chats(limit=10)
    assert [c["chat_key"] for c in chats][0] == "a"
    assert {c["chat_key"] for c in chats} == {"a", "b"}


def test_record_run_and_stats(store: LineRpaStateStore):
    # 纯空转（step 在跳过清单 & 无 peer/reply/error）不应写入
    store.record_run(
        chat_key="c1", ok=True, step="no_peer_text",
        peer_text=None, reply_text=None, reader_path="",
        total_ms=1.0, error=None,
    )
    assert store.recent_runs(limit=10) == []

    # 有对端文本 → 写入
    store.record_run(
        chat_key="c1", ok=True, step="sent",
        peer_text="hi", reply_text="hi there",
        reader_path="/tmp/x.xml", total_ms=1234.5,
    )
    runs = store.recent_runs(limit=10)
    assert len(runs) == 1
    assert runs[0]["peer_text"] == "hi"
    assert runs[0]["step"] == "sent"

    # 错误也应写入
    store.record_run(
        chat_key="c1", ok=False, step="send_failed",
        peer_text=None, reply_text=None,
        reader_path="", total_ms=0.0, error="adb offline",
    )

    stats = store.run_stats(window_hours=1.0)
    assert stats["total"] == 2
    assert stats["sent"] == 1
    assert 0.0 <= stats["ok_rate"] <= 100.0
    assert stats["avg_send_ms"] > 0


def test_meta_roundtrip(store: LineRpaStateStore):
    assert store.get_meta("k", default=None) is None
    store.set_meta("k", "v")
    assert store.get_meta("k") == "v"
    store.set_meta("obj", {"a": 1, "b": [1, 2]})
    assert store.get_meta("obj") == {"a": 1, "b": [1, 2]}


def test_runs_ring_eviction(tmp_path: Path):
    s = LineRpaStateStore(tmp_path / "r.db", max_runs_kept=50)
    try:
        for i in range(70):
            s.record_run(
                chat_key=f"c{i%3}", ok=True, step="sent",
                peer_text=f"peer{i}", reply_text="ok",
                reader_path="", total_ms=10.0,
            )
        runs = s.recent_runs(limit=200)
        assert len(runs) <= 50
    finally:
        s.close()


def test_migrate_from_legacy_json(tmp_path: Path):
    legacy = tmp_path / "line_rpa_state.json"
    legacy.write_text(
        '{"last_peer_text": "你好", "last_reply": "在的",'
        ' "last_screen_crop_sha256": "deadbeef"}',
        encoding="utf-8",
    )
    s = LineRpaStateStore(tmp_path / "line_rpa_state.db")
    try:
        ok = migrate_from_legacy_json(s, legacy)
        assert ok is True
        row = s.get_chat_state("line_rpa:default")
        assert row["last_peer_text"] == "你好"
        assert row["last_reply"] == "在的"
        assert row["last_screen_sha256"].startswith("deadbeef")
        # 第二次迁移应被跳过
        assert migrate_from_legacy_json(s, legacy) is False
    finally:
        s.close()
