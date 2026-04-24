"""一次性校验：配置 + SQLite 表 escalation_cooldown_by_norm。用法: python tools/verify_human_escalation_runtime.py"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.utils.config_manager import ConfigManager
from src.utils.human_escalation import HumanEscalationHelper, _escalation_cooldown_scope_legacy
from src.utils.human_escalation_store import HumanEscalationStore


async def main() -> int:
    cm = ConfigManager()
    await cm.load()
    cfg = cm.config or {}
    he = cfg.get("human_escalation") or {}
    cfg_dir = Path(cm.config_path).parent
    db = cfg_dir / "human_escalation.db"

    print("=== config human_escalation ===")
    print("  enabled:", he.get("enabled"))
    print("  repeat_threshold:", he.get("repeat_threshold"))
    print("  cooldown_sec:", he.get("cooldown_sec"))
    print("  escalation_cooldown_scope:", he.get("escalation_cooldown_scope") or "(default per_normalized_question)")
    print("  legacy_global_cooldown:", _escalation_cooldown_scope_legacy(he))
    agents = he.get("agents")
    n = len(agents) if isinstance(agents, list) else 0
    print("  agents count:", n)
    print("  db:", db, "exists=", db.is_file())

    if db.is_file():
        con = sqlite3.connect(str(db))
        row = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='escalation_cooldown_by_norm'"
        ).fetchone()
        con.close()
        print("  table escalation_cooldown_by_norm:", "OK" if row else "MISSING")
    else:
        print("  table escalation_cooldown_by_norm: (db missing, skip)")

    store = HumanEscalationStore(db)
    h = HumanEscalationHelper(cfg, store)
    c = h._cfg()
    print("=== Helper._cfg enabled ===", c.get("enabled"))
    print("=== verify_human_escalation_runtime OK ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
