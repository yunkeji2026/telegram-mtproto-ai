"""P14-B: tests for YAML-loadable intent_tags + reload helper."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.integrations import rpa_shared


def test_intent_tags_loaded_from_repo_yaml() -> None:
    """The shipped config/intent_tags.yaml should populate _INTENT_TAGS at import."""
    tags = rpa_shared._INTENT_TAGS
    assert "purchase" in tags
    assert "support" in tags
    assert "inquiry" in tags
    assert "greeting" in tags
    # Sanity: purchase keywords contain Chinese + English
    p = tags["purchase"]
    assert "买" in p
    assert "buy" in p
    # word-boundary still applied for short ASCII keywords
    assert rpa_shared.compute_intent_tag("this is a test") == "general"
    assert rpa_shared.compute_intent_tag("Hi there") == "greeting"


def test_reload_intent_tags_picks_up_yaml_change(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """Override INTENT_TAGS_PATH to a tmp file, mutate it, reload — runtime sees new keywords."""
    yaml_file = tmp_path / "custom_intent.yaml"
    yaml_file.write_text(
        "purchase:\n  - 特殊关键词xyz123\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("INTENT_TAGS_PATH", str(yaml_file))
    out = rpa_shared.reload_intent_tags()
    try:
        assert out["purchase"] == ["特殊关键词xyz123"]
        # Other categories are absent → fallback to "general" for greetings now
        assert rpa_shared.compute_intent_tag("特殊关键词xyz123 来了") == "purchase"
        assert rpa_shared.compute_intent_tag("hello") == "general"
    finally:
        # Restore default by clearing override and reloading
        monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
        rpa_shared.reload_intent_tags()


def test_reload_falls_back_when_yaml_missing(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """Pointing to a non-existent path should leave us with default tags, not crash."""
    monkeypatch.setenv("INTENT_TAGS_PATH", str(tmp_path / "does_not_exist.yaml"))
    out = rpa_shared.reload_intent_tags()
    try:
        # Falls back → has all 4 default categories
        assert {"purchase", "support", "inquiry", "greeting"}.issubset(set(out))
    finally:
        monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
        rpa_shared.reload_intent_tags()


def test_reload_falls_back_on_malformed_yaml(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("::: not yaml :::", encoding="utf-8")
    monkeypatch.setenv("INTENT_TAGS_PATH", str(bad))
    out = rpa_shared.reload_intent_tags()
    try:
        assert {"purchase", "support", "inquiry", "greeting"}.issubset(set(out))
    finally:
        monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
        rpa_shared.reload_intent_tags()


def test_reload_partial_skip_invalid_entries(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch,
                                              caplog: pytest.LogCaptureFixture) -> None:
    """P15-D: invalid tag entries are skipped with warning; valid ones still load."""
    yaml_file = tmp_path / "mixed.yaml"
    # `bad_tag` value is a string instead of list → skipped
    # `another_bad` value is empty list → skipped
    # `purchase` is valid → loaded
    yaml_file.write_text(
        "purchase:\n  - 买\n  - buy\n"
        "bad_tag: not_a_list\n"
        "another_bad: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("INTENT_TAGS_PATH", str(yaml_file))
    caplog.clear()
    with caplog.at_level("WARNING"):
        out = rpa_shared.reload_intent_tags()
    try:
        assert "purchase" in out
        assert "bad_tag" not in out
        assert "another_bad" not in out
        # Warning text mentions both skipped entries
        warns = " ".join(rec.message for rec in caplog.records)
        assert "bad_tag" in warns
        assert "another_bad" in warns
    finally:
        monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
        rpa_shared.reload_intent_tags()


def test_reload_top_level_list_falls_back(tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch,
                                           caplog: pytest.LogCaptureFixture) -> None:
    """P15-D: top-level list (instead of mapping) → fallback + descriptive warning."""
    yaml_file = tmp_path / "list.yaml"
    yaml_file.write_text("- buy\n- pay\n", encoding="utf-8")
    monkeypatch.setenv("INTENT_TAGS_PATH", str(yaml_file))
    caplog.clear()
    with caplog.at_level("WARNING"):
        out = rpa_shared.reload_intent_tags()
    try:
        assert {"purchase", "support", "inquiry", "greeting"}.issubset(set(out))
        msgs = " ".join(rec.message for rec in caplog.records)
        assert "list" in msgs.lower() or "mapping" in msgs.lower()
    finally:
        monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
        rpa_shared.reload_intent_tags()


# ────────────────────────────────────────────────────────────────────────
# P16-D: schema_version reserved key handling
# ────────────────────────────────────────────────────────────────────────


def test_schema_version_key_skipped_silently(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch,
                                              caplog: pytest.LogCaptureFixture) -> None:
    """schema_version is reserved — must be skipped without warning."""
    yaml_file = tmp_path / "with_schema.yaml"
    yaml_file.write_text(
        "schema_version: 1\n_meta: anything\npurchase:\n  - 买\n  - buy\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("INTENT_TAGS_PATH", str(yaml_file))
    caplog.clear()
    with caplog.at_level("WARNING"):
        out = rpa_shared.reload_intent_tags()
    try:
        assert "purchase" in out
        assert "schema_version" not in out
        assert "_meta" not in out
        # No warnings about schema_version (reserved → silent skip)
        warnings = " ".join(rec.message for rec in caplog.records)
        assert "schema_version" not in warnings
        assert "_meta" not in warnings
    finally:
        monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
        rpa_shared.reload_intent_tags()


# ────────────────────────────────────────────────────────────────────────
# P16-A: write_intent_tags_yaml + read_intent_tags_yaml
# ────────────────────────────────────────────────────────────────────────


def test_write_intent_tags_atomic_with_backup(tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """write should: validate → backup old → atomic replace → reload."""
    yaml_file = tmp_path / "intent_tags.yaml"
    yaml_file.write_text("purchase:\n  - 旧关键词\n", encoding="utf-8")
    monkeypatch.setenv("INTENT_TAGS_PATH", str(yaml_file))
    rpa_shared.reload_intent_tags()
    try:
        new_yaml = "schema_version: 1\npurchase:\n  - 新关键词\nsupport:\n  - help\n"
        result = rpa_shared.write_intent_tags_yaml(new_yaml)
        assert result["ok"] is True
        assert result["category_count"] == 2  # schema_version excluded
        assert result["keyword_count"] == 2
        assert result["backup_path"]  # backup created
        # Backup contains old content
        bak = Path(result["backup_path"])
        assert bak.exists()
        assert "旧关键词" in bak.read_text(encoding="utf-8")
        # File now has new content
        assert "新关键词" in yaml_file.read_text(encoding="utf-8")
        # Loaded into runtime
        assert "新关键词" in rpa_shared._INTENT_TAGS["purchase"]
        assert "help" in rpa_shared._INTENT_TAGS["support"]
    finally:
        monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
        rpa_shared.reload_intent_tags()


def test_write_intent_tags_rejects_invalid(tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """Bad YAML / wrong types should raise ValueError without touching the file."""
    yaml_file = tmp_path / "intent_tags.yaml"
    original = "purchase:\n  - 原始内容\n"
    yaml_file.write_text(original, encoding="utf-8")
    monkeypatch.setenv("INTENT_TAGS_PATH", str(yaml_file))
    rpa_shared.reload_intent_tags()
    try:
        # 1. YAML syntax error (unbalanced flow-style)
        with pytest.raises(ValueError, match="parse error"):
            rpa_shared.write_intent_tags_yaml("purchase: [a, b\n")
        assert "原始内容" in yaml_file.read_text(encoding="utf-8")  # untouched

        # 2. Top-level not a mapping
        with pytest.raises(ValueError, match="mapping"):
            rpa_shared.write_intent_tags_yaml("- a\n- b\n")
        assert "原始内容" in yaml_file.read_text(encoding="utf-8")

        # 3. Tag value not a list
        with pytest.raises(ValueError, match="must be a list"):
            rpa_shared.write_intent_tags_yaml("purchase: a string\n")
        assert "原始内容" in yaml_file.read_text(encoding="utf-8")

        # 4. No valid tags (only reserved keys / empty lists)
        with pytest.raises(ValueError, match="non-empty tag list"):
            rpa_shared.write_intent_tags_yaml("schema_version: 1\npurchase: []\n")
        assert "原始内容" in yaml_file.read_text(encoding="utf-8")

        # 5. Oversized content
        with pytest.raises(ValueError, match="200KB"):
            rpa_shared.write_intent_tags_yaml("x" * 300_000)
    finally:
        monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
        rpa_shared.reload_intent_tags()


def test_read_intent_tags_yaml(tmp_path: Path,
                                monkeypatch: pytest.MonkeyPatch) -> None:
    """read should return raw text or empty string when file missing."""
    yaml_file = tmp_path / "intent_tags.yaml"
    monkeypatch.setenv("INTENT_TAGS_PATH", str(yaml_file))
    try:
        # Missing → ""
        assert rpa_shared.read_intent_tags_yaml() == ""
        # Existing → raw text returned verbatim
        body = "purchase:\n  - 测试\n"
        yaml_file.write_text(body, encoding="utf-8")
        assert rpa_shared.read_intent_tags_yaml() == body
    finally:
        monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
        rpa_shared.reload_intent_tags()


# ────────────────────────────────────────────────────────────────────────
# P17-B: diff_intent_tags
# ────────────────────────────────────────────────────────────────────────


def test_diff_intent_tags_added_removed_changed(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """diff should split changes into added_tags / removed_tags / changed_tags."""
    yaml_file = tmp_path / "intent_tags.yaml"
    yaml_file.write_text(
        "purchase:\n  - 买\n  - buy\n"
        "support:\n  - 退款\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("INTENT_TAGS_PATH", str(yaml_file))
    rpa_shared.reload_intent_tags()
    try:
        new_yaml = (
            "purchase:\n  - 买\n  - 下单\n"           # +下单 -buy
            "inquiry:\n  - 请问\n"                     # +inquiry (new tag)
            # support removed entirely
        )
        d = rpa_shared.diff_intent_tags(new_yaml)
        assert d["ok"] is True
        assert d["added_tags"] == ["inquiry"]
        assert d["removed_tags"] == ["support"]
        assert "purchase" in d["changed_tags"]
        assert d["changed_tags"]["purchase"]["added"] == ["下单"]
        assert d["changed_tags"]["purchase"]["removed"] == ["buy"]
        # No-op diff for unchanged tags is omitted
        # purchase has changes → present; inquiry/support handled at category level
    finally:
        monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
        rpa_shared.reload_intent_tags()


def test_diff_intent_tags_no_changes(tmp_path: Path,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    yaml_file = tmp_path / "intent_tags.yaml"
    yaml_file.write_text("purchase:\n  - 买\n", encoding="utf-8")
    monkeypatch.setenv("INTENT_TAGS_PATH", str(yaml_file))
    rpa_shared.reload_intent_tags()
    try:
        d = rpa_shared.diff_intent_tags("purchase:\n  - 买\n")
        assert d["added_tags"] == []
        assert d["removed_tags"] == []
        assert d["changed_tags"] == {}
    finally:
        monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
        rpa_shared.reload_intent_tags()


def test_diff_intent_tags_invalid_raises(tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTENT_TAGS_PATH", str(tmp_path / "intent_tags.yaml"))
    try:
        with pytest.raises(ValueError):
            rpa_shared.diff_intent_tags("- a\n- b\n")  # top-level list
    finally:
        monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
        rpa_shared.reload_intent_tags()


# ────────────────────────────────────────────────────────────────────────
# P17-C: rotating backups
# ────────────────────────────────────────────────────────────────────────


def test_rotating_backups_keep_only_n(tmp_path: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """Multiple writes generate timestamped .bak* files; old ones get cleaned."""
    import time as _time
    yaml_file = tmp_path / "intent_tags.yaml"
    yaml_file.write_text("purchase:\n  - 初始\n", encoding="utf-8")
    monkeypatch.setenv("INTENT_TAGS_PATH", str(yaml_file))
    rpa_shared.reload_intent_tags()
    try:
        # Write 7 times — keep only last 5 backups
        for i in range(7):
            rpa_shared.write_intent_tags_yaml(f"purchase:\n  - kw{i}\n")
            _time.sleep(1.05)  # ensure distinct timestamp filename (precision = 1s)
        backups = list_intent_backups(tmp_path, yaml_file)
        # _INTENT_TAGS_BACKUP_KEEP = 5 → at most 5 backups
        assert len(backups) <= 5
        # The most recent backup contains the previous-to-last write
        # (last write didn't create backup of itself; the .bak with the latest
        #  mtime contains the content from before the *last* write_intent_tags_yaml)
        latest = backups[0]  # mtime desc
        latest_content = latest.read_text(encoding="utf-8")
        assert "kw5" in latest_content  # before the last write (which had kw6)
    finally:
        monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
        rpa_shared.reload_intent_tags()


def list_intent_backups(parent: Path, target: Path):
    """Helper: list .bak* files for the given target, mtime desc."""
    return sorted(
        [b for b in parent.iterdir() if b.name.startswith(target.name + ".bak")],
        key=lambda x: x.stat().st_mtime, reverse=True,
    )


# ────────────────────────────────────────────────────────────────────────
# P17-D: list_intent_tags_backups + restore_intent_tags_backup
# ────────────────────────────────────────────────────────────────────────


def test_restore_intent_tags_backup_round_trip(tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """Save A → save B → restore A → runtime back to A."""
    import time as _time
    yaml_file = tmp_path / "intent_tags.yaml"
    yaml_file.write_text("purchase:\n  - 旧\n", encoding="utf-8")
    monkeypatch.setenv("INTENT_TAGS_PATH", str(yaml_file))
    rpa_shared.reload_intent_tags()
    try:
        # First write — backs up the original
        rpa_shared.write_intent_tags_yaml("purchase:\n  - 中间\n")
        _time.sleep(1.05)
        # Second write — backs up the "中间" version
        rpa_shared.write_intent_tags_yaml("purchase:\n  - 最新\n")
        assert "最新" in rpa_shared._INTENT_TAGS["purchase"]

        # List backups
        backups = rpa_shared.list_intent_tags_backups()
        assert len(backups) >= 2
        # The most recent backup contains "中间" (saved when "最新" was being written)
        target_bak = backups[0]
        target_path = yaml_file.parent / target_bak["filename"]
        assert "中间" in target_path.read_text(encoding="utf-8")

        # Restore
        result = rpa_shared.restore_intent_tags_backup(target_bak["filename"])
        assert result["ok"] is True
        assert "中间" in rpa_shared._INTENT_TAGS["purchase"]
        # The "最新" version should now be in a fresh backup
        new_backups = rpa_shared.list_intent_tags_backups()
        latest_after = (yaml_file.parent / new_backups[0]["filename"]).read_text(encoding="utf-8")
        assert "最新" in latest_after
    finally:
        monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
        rpa_shared.reload_intent_tags()


def test_restore_rejects_path_traversal(tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """Path traversal attempts must be blocked."""
    yaml_file = tmp_path / "intent_tags.yaml"
    yaml_file.write_text("purchase:\n  - 测试\n", encoding="utf-8")
    monkeypatch.setenv("INTENT_TAGS_PATH", str(yaml_file))
    try:
        for evil in ["../etc/passwd", "/etc/passwd",
                     "..\\windows\\system32",
                     "intent_tags.yaml.bak/../../../passwd",
                     "other_file.yaml",     # not starting with intent_tags.yaml.bak
                     "",                     # empty
                     "intent_tags.yaml.bak\x00etc"]:  # null byte
            with pytest.raises(ValueError):
                rpa_shared.restore_intent_tags_backup(evil)
    finally:
        monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
        rpa_shared.reload_intent_tags()


# ────────────────────────────────────────────────────────────────────────
# P18-D: diff reorder detection
# ────────────────────────────────────────────────────────────────────────


def test_diff_detects_reorder_only(tmp_path: Path,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """Same keywords in different order → reordered_tags populated, no false changes."""
    yaml_file = tmp_path / "intent_tags.yaml"
    yaml_file.write_text("purchase:\n  - 买\n  - buy\n  - 下单\n", encoding="utf-8")
    monkeypatch.setenv("INTENT_TAGS_PATH", str(yaml_file))
    rpa_shared.reload_intent_tags()
    try:
        # Same content, different order → must be flagged as reordered
        d = rpa_shared.diff_intent_tags("purchase:\n  - 下单\n  - 买\n  - buy\n")
        assert d["added_tags"] == []
        assert d["removed_tags"] == []
        assert d["changed_tags"] == {}
        assert d["reordered_tags"] == ["purchase"]
        assert "reordered" in d["summary"]
    finally:
        monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
        rpa_shared.reload_intent_tags()


def test_diff_reorder_combined_with_add_remove(tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """If a tag both reorders and changes content, only changed_tags entry sets reordered=true."""
    yaml_file = tmp_path / "intent_tags.yaml"
    yaml_file.write_text("purchase:\n  - 买\n  - buy\n", encoding="utf-8")
    monkeypatch.setenv("INTENT_TAGS_PATH", str(yaml_file))
    rpa_shared.reload_intent_tags()
    try:
        # New: order swapped + added one
        d = rpa_shared.diff_intent_tags("purchase:\n  - buy\n  - 买\n  - 新词\n")
        assert "purchase" in d["changed_tags"]
        assert d["changed_tags"]["purchase"]["added"] == ["新词"]
        assert d["changed_tags"]["purchase"]["removed"] == []
        assert d["changed_tags"]["purchase"].get("reordered") is True
        # Not duplicated in reordered_tags (only true reorder-only there)
        assert "purchase" not in d["reordered_tags"]
    finally:
        monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
        rpa_shared.reload_intent_tags()


# ────────────────────────────────────────────────────────────────────────
# P18-A: read_intent_tags_backup (dry-run path helper)
# ────────────────────────────────────────────────────────────────────────


def test_read_intent_tags_backup_round_trip(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """read_intent_tags_backup returns content verbatim for valid backup."""
    yaml_file = tmp_path / "intent_tags.yaml"
    yaml_file.write_text("purchase:\n  - 原始\n", encoding="utf-8")
    monkeypatch.setenv("INTENT_TAGS_PATH", str(yaml_file))
    rpa_shared.reload_intent_tags()
    try:
        rpa_shared.write_intent_tags_yaml("purchase:\n  - 新内容\n")
        backups = rpa_shared.list_intent_tags_backups()
        assert backups
        content = rpa_shared.read_intent_tags_backup(backups[0]["filename"])
        assert "原始" in content
    finally:
        monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
        rpa_shared.reload_intent_tags()


def test_read_intent_tags_backup_rejects_traversal(tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    yaml_file = tmp_path / "intent_tags.yaml"
    yaml_file.write_text("purchase:\n  - x\n", encoding="utf-8")
    monkeypatch.setenv("INTENT_TAGS_PATH", str(yaml_file))
    try:
        with pytest.raises(ValueError):
            rpa_shared.read_intent_tags_backup("../etc/passwd")
        with pytest.raises(ValueError):
            rpa_shared.read_intent_tags_backup("")
    finally:
        monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
        rpa_shared.reload_intent_tags()


# ────────────────────────────────────────────────────────────────────────
# P18-B: edit stats counters
# ────────────────────────────────────────────────────────────────────────


def test_edit_stats_counters(tmp_path: Path,
                              monkeypatch: pytest.MonkeyPatch) -> None:
    """writes / reloads / restores / last_edit_ts updated by their operations."""
    import time as _time
    yaml_file = tmp_path / "intent_tags.yaml"
    yaml_file.write_text("purchase:\n  - 初始\n", encoding="utf-8")
    monkeypatch.setenv("INTENT_TAGS_PATH", str(yaml_file))
    # Snapshot baseline (other tests may have bumped counters)
    before = rpa_shared.get_intent_tags_edit_stats()
    try:
        # First reload (manual) — bumps reloads
        rpa_shared.reload_intent_tags()
        after_reload = rpa_shared.get_intent_tags_edit_stats()
        assert after_reload["reloads"] == before["reloads"] + 1

        # Write — bumps writes + last_edit_ts + reloads (cascade)
        t0 = _time.time()
        rpa_shared.write_intent_tags_yaml("purchase:\n  - 改\n")
        after_write = rpa_shared.get_intent_tags_edit_stats()
        assert after_write["writes"] == before["writes"] + 1
        assert after_write["last_edit_ts"] >= t0

        # Restore — bumps restores + writes(cascade)
        _time.sleep(1.05)
        rpa_shared.write_intent_tags_yaml("purchase:\n  - 中间\n")  # create another backup
        backups = rpa_shared.list_intent_tags_backups()
        assert backups
        before_restore = rpa_shared.get_intent_tags_edit_stats()
        rpa_shared.restore_intent_tags_backup(backups[0]["filename"])
        after_restore = rpa_shared.get_intent_tags_edit_stats()
        assert after_restore["restores"] == before_restore["restores"] + 1
        assert after_restore["writes"] == before_restore["writes"] + 1
    finally:
        monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
        rpa_shared.reload_intent_tags()


def test_edit_stats_immutable_snapshot(tmp_path: Path) -> None:
    """get_intent_tags_edit_stats returns a copy — caller can't mutate internal state."""
    s1 = rpa_shared.get_intent_tags_edit_stats()
    s1["writes"] = 99999
    s2 = rpa_shared.get_intent_tags_edit_stats()
    assert s2["writes"] != 99999


# ────────────────────────────────────────────────────────────────────────
# P19-A: ms-precision backups + rapid-fire safety
# ────────────────────────────────────────────────────────────────────────


def test_ms_precision_backups_no_collision(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """Rapid consecutive writes should produce distinct backup files (no overwriting)."""
    yaml_file = tmp_path / "intent_tags.yaml"
    yaml_file.write_text("purchase:\n  - 初始\n", encoding="utf-8")
    monkeypatch.setenv("INTENT_TAGS_PATH", str(yaml_file))
    rpa_shared.reload_intent_tags()
    try:
        # Write 5 times in a tight loop — no sleep
        for i in range(5):
            rpa_shared.write_intent_tags_yaml(f"purchase:\n  - kw{i}\n")
        # All distinct files preserved (up to KEEP=5)
        backups = sorted(b for b in tmp_path.iterdir()
                         if b.name.startswith(yaml_file.name + ".bak"))
        assert len(backups) >= 2  # at least we have multiple backups
        # Filenames must be unique
        assert len({b.name for b in backups}) == len(backups)
        # New format contains underscore-separated ms
        assert any("_" in b.name.split(".bak", 1)[1] for b in backups)
    finally:
        monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
        rpa_shared.reload_intent_tags()


def test_ms_precision_backup_filename_format(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """New backup file follows .bakYYYYMMDD_HHMMSS_mmm pattern."""
    import re as _re
    yaml_file = tmp_path / "intent_tags.yaml"
    yaml_file.write_text("purchase:\n  - x\n", encoding="utf-8")
    monkeypatch.setenv("INTENT_TAGS_PATH", str(yaml_file))
    rpa_shared.reload_intent_tags()
    try:
        result = rpa_shared.write_intent_tags_yaml("purchase:\n  - y\n")
        bak = Path(result["backup_path"])
        # Pattern: intent_tags.yaml.bak<14 digits>_<3 digits>[_<n>]
        assert _re.search(r"\.bak\d{8}_\d{6}_\d{3}", bak.name)
    finally:
        monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
        rpa_shared.reload_intent_tags()


# ────────────────────────────────────────────────────────────────────────
# P19-C: edits_1h sliding window
# ────────────────────────────────────────────────────────────────────────


def test_edits_1h_counts_recent_writes(tmp_path: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """edits_1h reflects writes from the past hour."""
    yaml_file = tmp_path / "intent_tags.yaml"
    yaml_file.write_text("purchase:\n  - 初始\n", encoding="utf-8")
    monkeypatch.setenv("INTENT_TAGS_PATH", str(yaml_file))
    # Clear any leftover window from other tests
    rpa_shared._INTENT_TAGS_EDIT_WINDOW.clear()
    rpa_shared.reload_intent_tags()
    try:
        before = rpa_shared.get_intent_tags_edit_stats()["edits_1h"]
        rpa_shared.write_intent_tags_yaml("purchase:\n  - a\n")
        rpa_shared.write_intent_tags_yaml("purchase:\n  - b\n")
        rpa_shared.write_intent_tags_yaml("purchase:\n  - c\n")
        after = rpa_shared.get_intent_tags_edit_stats()["edits_1h"]
        assert after == before + 3
    finally:
        monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
        rpa_shared._INTENT_TAGS_EDIT_WINDOW.clear()
        rpa_shared.reload_intent_tags()


def test_edits_1h_expires_old_entries(tmp_path: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """Entries older than 1h must drop out of the window."""
    import time as _time
    yaml_file = tmp_path / "intent_tags.yaml"
    yaml_file.write_text("purchase:\n  - x\n", encoding="utf-8")
    monkeypatch.setenv("INTENT_TAGS_PATH", str(yaml_file))
    rpa_shared._INTENT_TAGS_EDIT_WINDOW.clear()
    rpa_shared.reload_intent_tags()
    try:
        # Inject a fake old timestamp (> 1h ago) + a fresh one
        now = _time.time()
        rpa_shared._INTENT_TAGS_EDIT_WINDOW.append(now - 4000)  # ~67min ago
        rpa_shared._INTENT_TAGS_EDIT_WINDOW.append(now - 60)    # 1min ago
        es = rpa_shared.get_intent_tags_edit_stats()
        # Old entry pruned, only the recent one counts
        assert es["edits_1h"] == 1
    finally:
        monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
        rpa_shared._INTENT_TAGS_EDIT_WINDOW.clear()
        rpa_shared.reload_intent_tags()


# ────────────────────────────────────────────────────────────────────────
# P19-D: normcase containment (Windows case-insensitive paths)
# ────────────────────────────────────────────────────────────────────────


def test_safe_backup_path_case_insensitive_on_windows(tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows, uppercase variants of valid backup names should still resolve."""
    import sys
    yaml_file = tmp_path / "intent_tags.yaml"
    yaml_file.write_text("purchase:\n  - x\n", encoding="utf-8")
    monkeypatch.setenv("INTENT_TAGS_PATH", str(yaml_file))
    rpa_shared.reload_intent_tags()
    try:
        result = rpa_shared.write_intent_tags_yaml("purchase:\n  - y\n")
        bak_name = Path(result["backup_path"]).name
        if sys.platform == "win32":
            # Uppercase variant should work (Windows FS is case-insensitive)
            upper_name = bak_name.upper()
            content = rpa_shared.read_intent_tags_backup(upper_name)
            assert content
        else:
            # On Linux, case-sensitive — uppercase variant should fail (file doesn't exist)
            # But _safe_backup_path itself should accept the prefix (since normcase = identity)
            # The "file not found" branch handles it
            with pytest.raises(ValueError):
                rpa_shared.read_intent_tags_backup(bak_name.upper())
    finally:
        monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
        rpa_shared.reload_intent_tags()


# ────────────────────────────────────────────────────────────────────────
# P20-A: thread-safe concurrent mutations
# ────────────────────────────────────────────────────────────────────────


def test_concurrent_writes_no_lost_updates(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """Multiple threads writing concurrently — no counter loss, no deque corruption."""
    import threading
    yaml_file = tmp_path / "intent_tags.yaml"
    yaml_file.write_text("purchase:\n  - 初始\n", encoding="utf-8")
    monkeypatch.setenv("INTENT_TAGS_PATH", str(yaml_file))
    rpa_shared._INTENT_TAGS_EDIT_WINDOW.clear()
    rpa_shared.reload_intent_tags()
    before = rpa_shared.get_intent_tags_edit_stats()
    try:
        N = 20
        errors: list = []

        def worker(idx: int) -> None:
            try:
                rpa_shared.write_intent_tags_yaml(f"purchase:\n  - kw_t{idx}\n")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert not errors, f"thread errors: {errors}"
        after = rpa_shared.get_intent_tags_edit_stats()
        # writes counter incremented N times (no race losses)
        assert after["writes"] == before["writes"] + N
        # edits_1h reflects all writes
        assert after["edits_1h"] >= N
    finally:
        monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
        rpa_shared._INTENT_TAGS_EDIT_WINDOW.clear()
        rpa_shared.reload_intent_tags()


def test_concurrent_reads_writes_safe(tmp_path: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """get_intent_tags_edit_stats while writes happen — no deque mutation crash."""
    import threading
    yaml_file = tmp_path / "intent_tags.yaml"
    yaml_file.write_text("purchase:\n  - x\n", encoding="utf-8")
    monkeypatch.setenv("INTENT_TAGS_PATH", str(yaml_file))
    rpa_shared._INTENT_TAGS_EDIT_WINDOW.clear()
    rpa_shared.reload_intent_tags()
    try:
        stop = threading.Event()
        crashes: list = []

        def reader():
            while not stop.is_set():
                try:
                    rpa_shared.get_intent_tags_edit_stats()
                except Exception as e:
                    crashes.append(e)

        def writer(i: int):
            try:
                rpa_shared.write_intent_tags_yaml(f"purchase:\n  - kw{i}\n")
            except Exception as e:
                crashes.append(e)

        readers = [threading.Thread(target=reader) for _ in range(3)]
        writers = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
        for t in readers: t.start()
        for t in writers: t.start()
        for t in writers: t.join()
        stop.set()
        for t in readers: t.join()
        assert not crashes
    finally:
        monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
        rpa_shared._INTENT_TAGS_EDIT_WINDOW.clear()
        rpa_shared.reload_intent_tags()


# ────────────────────────────────────────────────────────────────────────
# P20-C: counter persistence to sidecar JSON
# ────────────────────────────────────────────────────────────────────────


def test_stats_persistence_round_trip(tmp_path: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """Write → check sidecar exists → reload → counters restored."""
    yaml_file = tmp_path / "intent_tags.yaml"
    yaml_file.write_text("purchase:\n  - 初始\n", encoding="utf-8")
    monkeypatch.setenv("INTENT_TAGS_PATH", str(yaml_file))
    rpa_shared._INTENT_TAGS_EDIT_WINDOW.clear()
    rpa_shared.reload_intent_tags()
    try:
        # Reset to baseline by faking a clean state for this isolated test
        rpa_shared._INTENT_TAGS_EDIT_STATS["writes"] = 0
        rpa_shared._INTENT_TAGS_EDIT_STATS["reloads"] = 0
        rpa_shared._INTENT_TAGS_EDIT_STATS["restores"] = 0

        rpa_shared.write_intent_tags_yaml("purchase:\n  - 新\n")
        rpa_shared.write_intent_tags_yaml("purchase:\n  - 更新\n")
        sp = rpa_shared._intent_tags_stats_path()
        assert sp.exists(), "stats sidecar not created"

        import json as _json
        data = _json.loads(sp.read_text(encoding="utf-8"))
        assert data["writes"] == 2
        assert data["last_edit_ts"] > 0
        assert isinstance(data["edit_window"], list)
        assert len(data["edit_window"]) == 2

        # Simulate restart: clear in-memory state, then call loader
        rpa_shared._INTENT_TAGS_EDIT_STATS["writes"] = 0
        rpa_shared._INTENT_TAGS_EDIT_STATS["reloads"] = 0
        rpa_shared._INTENT_TAGS_EDIT_STATS["restores"] = 0
        rpa_shared._INTENT_TAGS_EDIT_STATS["last_edit_ts"] = 0.0
        rpa_shared._INTENT_TAGS_EDIT_WINDOW.clear()
        rpa_shared._load_stats_persistent()
        after = rpa_shared.get_intent_tags_edit_stats()
        assert after["writes"] == 2
        assert after["edits_1h"] == 2
        assert after["last_edit_ts"] > 0
    finally:
        monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
        rpa_shared._INTENT_TAGS_EDIT_WINDOW.clear()
        rpa_shared.reload_intent_tags()


def test_stats_load_drops_expired_window_entries(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """Sidecar with timestamps > 1h old → those entries pruned on load."""
    import json as _json, time as _t
    yaml_file = tmp_path / "intent_tags.yaml"
    yaml_file.write_text("purchase:\n  - x\n", encoding="utf-8")
    monkeypatch.setenv("INTENT_TAGS_PATH", str(yaml_file))
    try:
        sp = rpa_shared._intent_tags_stats_path()
        now = _t.time()
        sp.write_text(_json.dumps({
            "writes": 5, "reloads": 3, "restores": 1, "last_edit_ts": now - 30,
            "edit_window": [now - 5000, now - 60, now - 10],  # first one > 1h ago
        }), encoding="utf-8")
        rpa_shared._INTENT_TAGS_EDIT_WINDOW.clear()
        rpa_shared._INTENT_TAGS_EDIT_STATS["writes"] = 0
        rpa_shared._load_stats_persistent()
        es = rpa_shared.get_intent_tags_edit_stats()
        assert es["writes"] == 5  # counter restored verbatim
        # Only 2 of 3 ts in window (oldest pruned)
        assert es["edits_1h"] == 2
    finally:
        monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
        rpa_shared._INTENT_TAGS_EDIT_WINDOW.clear()
        rpa_shared.reload_intent_tags()


def test_persistence_throttle_skips_rapid_calls(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """P21-D: With >0 throttle interval, rapid persist calls do not all write."""
    import time as _t
    yaml_file = tmp_path / "intent_tags.yaml"
    yaml_file.write_text("purchase:\n  - x\n", encoding="utf-8")
    monkeypatch.setenv("INTENT_TAGS_PATH", str(yaml_file))
    # Re-enable throttle (conftest sets it to 0 for normal tests)
    monkeypatch.setattr(rpa_shared, "_STATS_SAVE_MIN_INTERVAL_SEC", 0.5)
    rpa_shared._stats_last_save_ts = 0.0
    try:
        sp = rpa_shared._intent_tags_stats_path()
        # First save: writes
        rpa_shared._save_stats_persistent()
        assert sp.exists()
        first_mtime = sp.stat().st_mtime

        # Immediate second save: should skip (throttle)
        _t.sleep(0.05)
        # Make in-memory change so we can detect if a real write happened
        rpa_shared._INTENT_TAGS_EDIT_STATS["writes"] = 999
        rpa_shared._save_stats_persistent()
        # File not changed
        assert sp.stat().st_mtime == first_mtime

        # force=True overrides
        rpa_shared._save_stats_persistent(force=True)
        # Wait a moment to be sure mtime resolution caught up
        if sp.stat().st_mtime == first_mtime:
            # Tight loop on the same mtime tick — touch text to verify content updated
            data = json.loads(sp.read_text(encoding="utf-8"))
            assert data["writes"] == 999
    finally:
        monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
        rpa_shared.reload_intent_tags()


def test_should_log_failure_exponential_sequence() -> None:
    """P23-C: _should_log_failure(n) emits on 3, 10, 30, 100, 300, 1000, 3000, ..."""
    expected_true = {3, 10, 30, 100, 300, 1000, 3000, 10000, 30000}
    for n in range(1, 100):
        actual = rpa_shared._should_log_failure(n)
        expected = n in expected_true
        assert actual == expected, f"n={n}: expected {expected} got {actual}"
    # Spot-check large values
    for n in expected_true:
        assert rpa_shared._should_log_failure(n), f"missed threshold: {n}"
    # Non-threshold large values
    for n in (4, 11, 99, 101, 999, 1001):
        assert not rpa_shared._should_log_failure(n), f"false positive at {n}"


def test_persistence_failure_tracking(tmp_path: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """P22-C: Save failure bumps consecutive + total; recovery clears consecutive."""
    yaml_file = tmp_path / "intent_tags.yaml"
    yaml_file.write_text("purchase:\n  - x\n", encoding="utf-8")
    monkeypatch.setenv("INTENT_TAGS_PATH", str(yaml_file))
    # Reset failure counters for clean test
    rpa_shared._stats_save_failures["consecutive"] = 0
    rpa_shared._stats_save_failures["total"] = 0
    rpa_shared._stats_save_failures["last_error"] = ""
    rpa_shared._stats_save_failures["last_failure_ts"] = 0.0
    try:
        # Make sidecar parent read-only or unwritable by monkeypatching write_text
        original_write = Path.write_text
        call_count = {"n": 0}

        def failing_write(self, *args, **kwargs):
            call_count["n"] += 1
            if "stats.json.tmp" in str(self) and call_count["n"] <= 2:
                raise OSError("simulated disk error")
            return original_write(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", failing_write)
        # First two writes fail
        rpa_shared._save_stats_persistent(force=True)
        rpa_shared._save_stats_persistent(force=True)
        es = rpa_shared.get_intent_tags_edit_stats()
        assert es["save_failures_consecutive"] == 2
        assert es["save_failures_total"] == 2
        assert "simulated disk error" in rpa_shared._stats_save_failures["last_error"]
        # Third write succeeds → consecutive resets to 0, total stays at 2
        rpa_shared._save_stats_persistent(force=True)
        es2 = rpa_shared.get_intent_tags_edit_stats()
        assert es2["save_failures_consecutive"] == 0
        assert es2["save_failures_total"] == 2  # cumulative
    finally:
        monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
        rpa_shared._stats_save_failures["consecutive"] = 0
        rpa_shared._stats_save_failures["total"] = 0
        rpa_shared.reload_intent_tags()


def test_persistence_disabled_hook_skips_all(tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """P21-D: _DISABLE_STATS_PERSISTENCE bypasses sidecar entirely."""
    yaml_file = tmp_path / "intent_tags.yaml"
    yaml_file.write_text("purchase:\n  - x\n", encoding="utf-8")
    monkeypatch.setenv("INTENT_TAGS_PATH", str(yaml_file))
    monkeypatch.setattr(rpa_shared, "_DISABLE_STATS_PERSISTENCE", True)
    try:
        sp = rpa_shared._intent_tags_stats_path()
        # Even ensure sidecar doesn't pre-exist
        if sp.exists():
            sp.unlink()
        rpa_shared.write_intent_tags_yaml("purchase:\n  - y\n")
        assert not sp.exists(), "persistence should be disabled but sidecar was created"
    finally:
        monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
        rpa_shared.reload_intent_tags()


def test_stats_load_handles_corrupt_sidecar(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed JSON / wrong types should fall back to defaults, not crash."""
    yaml_file = tmp_path / "intent_tags.yaml"
    yaml_file.write_text("purchase:\n  - x\n", encoding="utf-8")
    monkeypatch.setenv("INTENT_TAGS_PATH", str(yaml_file))
    try:
        sp = rpa_shared._intent_tags_stats_path()
        sp.write_text("not json{{{", encoding="utf-8")
        # Should not raise
        rpa_shared._load_stats_persistent()

        # Wrong type at top level
        sp.write_text('["a","list"]', encoding="utf-8")
        rpa_shared._load_stats_persistent()

        # Negative writes / wrong type — silently ignored
        import json as _json
        sp.write_text(_json.dumps({"writes": -5, "reloads": "no", "last_edit_ts": "bad"}),
                       encoding="utf-8")
        rpa_shared._load_stats_persistent()
    finally:
        monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
        rpa_shared.reload_intent_tags()


def test_safe_backup_path_rejects_resolved_outside_dir(tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """Even if filename passes char filter, resolved path outside parent should be rejected.

    Construct: filename = 'intent_tags.yaml.bak_escape' valid by char check; create as symlink
    to a file outside the parent. resolve() should escape, P19-D containment check rejects.
    """
    import sys, os as _os
    yaml_file = tmp_path / "intent_tags.yaml"
    yaml_file.write_text("purchase:\n  - x\n", encoding="utf-8")
    monkeypatch.setenv("INTENT_TAGS_PATH", str(yaml_file))
    rpa_shared.reload_intent_tags()
    try:
        # Create target file outside parent dir
        outside_dir = tmp_path.parent / "outside_xprof_test"
        outside_dir.mkdir(exist_ok=True)
        outside = outside_dir / "secret.txt"
        outside.write_text("secret", encoding="utf-8")
        symlink_path = tmp_path / "intent_tags.yaml.bak_escape"
        try:
            _os.symlink(str(outside), str(symlink_path))
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this filesystem/permissions")
        # Symlink in same dir but resolves outside → must be rejected
        with pytest.raises(ValueError, match="escapes backup directory"):
            rpa_shared.read_intent_tags_backup("intent_tags.yaml.bak_escape")
        # Cleanup
        try: symlink_path.unlink()
        except Exception: pass
        try: outside.unlink(); outside_dir.rmdir()
        except Exception: pass
    finally:
        monkeypatch.delenv("INTENT_TAGS_PATH", raising=False)
        rpa_shared.reload_intent_tags()
