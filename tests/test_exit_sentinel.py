"""进程退出可观测（哨兵 + 退出原因）——2026-07-12 无痕死亡排障配套。

只测纯文件/状态机语义（signal/atexit 的进程级行为不适合单测进程内验证）：
- 首启无哨兵 → None，写入本次哨兵；
- 残留哨兵 → 报上次现场（pid/存活时长），随后被本次覆写；
- 坏哨兵内容（空/非 JSON）→ 不抛，仍按残留报告；
- install 幂等（第二次调用 no-op）。
"""

import json
import time

import src.utils.exit_sentinel as ES


def _reset():
    ES._installed = False


def test_first_boot_no_sentinel(tmp_path):
    _reset()
    s = tmp_path / "run_sentinel.json"
    prev = ES.install(sentinel_path=str(s), fatal_log_path=str(tmp_path / "fatal.log"))
    assert prev is None                      # 首启：无上次现场
    data = json.loads(s.read_text(encoding="utf-8"))
    assert data["pid"] > 0 and data["started_at"] > 0   # 本次哨兵已写


def test_stale_sentinel_reports_previous_crash(tmp_path):
    _reset()
    s = tmp_path / "run_sentinel.json"
    s.write_text(json.dumps({"pid": 12345, "started_at": time.time() - 600}),
                 encoding="utf-8")
    prev = ES.install(sentinel_path=str(s), fatal_log_path=str(tmp_path / "fatal.log"))
    assert prev is not None and prev["pid"] == 12345
    assert prev["lived_sec"] > 0             # 存活时长可估
    # 本次哨兵已覆写为当前 pid
    assert json.loads(s.read_text(encoding="utf-8"))["pid"] != 12345


def test_corrupt_sentinel_does_not_raise(tmp_path):
    _reset()
    s = tmp_path / "run_sentinel.json"
    s.write_text("not-json{{", encoding="utf-8")
    prev = ES.install(sentinel_path=str(s), fatal_log_path=str(tmp_path / "fatal.log"))
    assert prev is not None                  # 残留即报告（内容缺失字段容忍）
    assert prev["pid"] is None


def test_install_idempotent(tmp_path):
    _reset()
    s = tmp_path / "run_sentinel.json"
    ES.install(sentinel_path=str(s), fatal_log_path=str(tmp_path / "fatal.log"))
    first = s.read_text(encoding="utf-8")
    assert ES.install(sentinel_path=str(s)) is None   # 幂等：第二次 no-op
    assert s.read_text(encoding="utf-8") == first     # 哨兵未被重写
