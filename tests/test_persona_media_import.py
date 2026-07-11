"""旧文件相册 → persona_media 注册表 导入器门禁：发现/去重幂等/dry-run/触发词/根目录归属。"""
from pathlib import Path

import pytest

from src.companion.persona_media_import import (
    discover_albums, import_albums, import_file)
from src.companion.persona_media_store import PersonaMediaStore


@pytest.fixture()
def store():
    return PersonaMediaStore(":memory:")


def _seed(root: Path):
    """造两个人设子目录 + 一张根目录散图。"""
    (root / "lin").mkdir(parents=True)
    (root / "mia").mkdir(parents=True)
    (root / "lin" / "a.jpg").write_bytes(b"lin-a")
    (root / "lin" / "b.png").write_bytes(b"lin-b")
    (root / "lin" / "notes.txt").write_bytes(b"ignored")  # 非媒体，跳过
    (root / "mia" / "c.mp4").write_bytes(b"mia-c")
    (root / "loose.jpg").write_bytes(b"root-loose")  # 根目录散图


def test_discover_subdirs_only(tmp_path):
    _seed(tmp_path)
    found = dict(discover_albums(tmp_path))
    assert set(found) == {"lin", "mia"}
    assert [f.name for f in found["lin"]] == ["a.jpg", "b.png"]  # txt 被过滤
    assert [f.name for f in found["mia"]] == ["c.mp4"]


def test_discover_root_files_only_with_persona(tmp_path):
    _seed(tmp_path)
    found = dict(discover_albums(tmp_path, only_persona="lin"))
    # 只 lin 子目录 + 根目录散图归 lin
    names = [f.name for f in found["lin"]]
    assert "a.jpg" in names and "loose.jpg" in names
    assert "mia" not in found


def test_import_apply_and_media_types(tmp_path, store):
    _seed(tmp_path)
    album_root = tmp_path / "static"
    summary = import_albums(store, tmp_path, album_root, apply=True)
    assert summary["apply"] is True
    assert summary["total_imported"] == 3  # lin:2 + mia:1
    assert store.stats("lin")["photo"] == 2
    assert store.stats("mia")["video"] == 1
    # 文件真复制进 static/<pid>/
    lin_items = store.list("lin")
    for it in lin_items:
        assert Path(it["file_path"]).is_file()
        assert it["url"].startswith("/static/persona_albums/lin/")


def test_import_idempotent_dedup(tmp_path, store):
    _seed(tmp_path)
    album_root = tmp_path / "static"
    import_albums(store, tmp_path, album_root, apply=True)
    again = import_albums(store, tmp_path, album_root, apply=True)
    assert again["total_imported"] == 0
    assert again["total_dup"] == 3  # 全部命中去重
    assert store.stats(None)["total"] == 3  # 未翻倍


def test_dry_run_does_not_write(tmp_path, store):
    _seed(tmp_path)
    album_root = tmp_path / "static"
    summary = import_albums(store, tmp_path, album_root, apply=False)
    assert summary["apply"] is False and summary["total_imported"] == 3
    assert store.stats(None)["total"] == 0  # 库里没落
    assert not album_root.exists()  # 没落盘


def test_import_with_triggers(tmp_path, store):
    _seed(tmp_path)
    import_albums(store, tmp_path, tmp_path / "static",
                  only_persona="lin", triggers=["自拍", "selfie"], apply=True)
    for it in store.list("lin"):
        assert it["triggers"] == ["自拍", "selfie"]


def test_import_file_skips_non_media(tmp_path, store):
    p = tmp_path / "x.txt"
    p.write_bytes(b"hi")
    assert import_file(store, tmp_path / "static", "lin", p, apply=True) == "skip"
