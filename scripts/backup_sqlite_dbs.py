#!/usr/bin/env python3
"""
将 config 目录下 SQLite 库备份到 config/backups/YYYYMMDD_HHMMSS/。
优先使用 SQLite backup API 生成一致快照（WAL 下更安全）；失败时回退为文件复制并打印告警。
用法：在项目根目录执行  python scripts/backup_sqlite_dbs.py
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# 项目根：scripts/ 上一级
ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "config"
PATTERNS = ("*.db",)


def _backup_one_sqlite(src: Path, dest: Path) -> None:
    """使用 connection.backup 写入目标文件（在线库亦可得到一致快照）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    src_conn = sqlite3.connect(str(src), timeout=30)
    try:
        dest_conn = sqlite3.connect(str(dest), timeout=30)
        try:
            src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        src_conn.close()


def backup_one_file(src: Path, dest: Path) -> str:
    """
    备份单个 .db 文件。返回 'sqlite' 或 'copy'，表示所用方式。
    供测试与外部工具导入（importlib 加载本脚本）。
    """
    try:
        _backup_one_sqlite(src, dest)
        return "sqlite"
    except Exception as e:
        print(
            "WARN: sqlite backup failed, fallback to copy2:",
            src.name,
            e,
            file=sys.stderr,
        )
        shutil.copy2(src, dest)
        return "copy"


def main() -> int:
    if not CFG.is_dir():
        print("ERROR: config 目录不存在:", CFG, file=sys.stderr)
        return 1
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = CFG / "backups" / ts
    out.mkdir(parents=True, exist_ok=True)
    n = 0
    modes: dict[str, int] = {"sqlite": 0, "copy": 0}
    for pat in PATTERNS:
        for f in sorted(CFG.glob(pat)):
            if f.parent != CFG:
                continue
            dest = out / f.name
            mode = backup_one_file(f, dest)
            modes[mode] = modes.get(mode, 0) + 1
            n += 1
            print("backed up", f.name, "->", dest.relative_to(ROOT), f"({mode})")
    if n == 0:
        print("WARN: no *.db files under config/")
        return 0
    print(
        "OK:",
        n,
        "files to",
        str(out.relative_to(ROOT)),
        f"[sqlite={modes.get('sqlite', 0)} copy={modes.get('copy', 0)}]",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
