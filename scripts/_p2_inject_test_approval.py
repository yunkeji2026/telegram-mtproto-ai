"""注入一条 pending approval 并把 created_at 调到 20 分钟前，
用于验证 SLA 循环 + 批量操作 + UI 展示。用完后 cleanup 脚本会删掉。"""
from __future__ import annotations

import pathlib
import sqlite3
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.integrations.messenger_rpa.state_store import (
    MessengerRpaStateStore, default_state_db_path,
)
from src.utils.config_manager import ConfigManager

cm = ConfigManager(config_path="config/config.yaml")
cm.load()
db = default_state_db_path(cm.config_path)
s = MessengerRpaStateStore(db, max_runs_kept=500)

aid = s.enqueue_approval(
    chat_key="messenger_rpa:__p2_test_aged__",
    chat_name="__P2_SLA_TEST__",
    peer_text="test peer — this is an aged pending approval for SLA verification",
    peer_kind="text",
    reply_text="ai draft reply (safe to reject in bulk)",
    screenshot_path="",
    run_id="p2test-sla",
    extra={"__p2_test__": True},
)
# 手动改 created_at 到 20 分钟前（> threshold_sec=600）
with sqlite3.connect(db) as c:
    c.execute(
        "UPDATE messenger_rpa_approvals SET created_at=? WHERE id=?",
        (time.time() - 1200, aid),
    )
    c.commit()
print(f"injected aged approval id={aid}, age=1200s")
