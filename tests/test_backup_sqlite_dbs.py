"""scripts/backup_sqlite_dbs.py：单文件备份与数据一致性。"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path


def _load_backup_module():
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "backup_sqlite_dbs", root / "scripts" / "backup_sqlite_dbs.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_backup_one_file_sqlite_roundtrip(tmp_path):
    mod = _load_backup_module()
    src = tmp_path / "src.db"
    dest = tmp_path / "out" / "dest.db"
    conn = sqlite3.connect(str(src))
    conn.execute("CREATE TABLE t(x INTEGER);")
    conn.execute("INSERT INTO t VALUES (42);")
    conn.commit()
    conn.close()
    mode = mod.backup_one_file(src, dest)
    assert mode == "sqlite"
    assert dest.is_file()
    c2 = sqlite3.connect(str(dest))
    row = c2.execute("SELECT x FROM t").fetchone()
    c2.close()
    assert row == (42,)
