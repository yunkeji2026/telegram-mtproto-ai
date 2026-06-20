"""Phase J：授权席位强制（seat enforcement）单测。

此前 `seat_exceeded` 已定义但无人调用——本阶段把它经 `seat_block_on_online` 接到
坐席「上线」(POST /api/workspace/presence) 边界。验证纯函数语义 + enforce 开关下的拦截/放行。
"""
from src.licensing.gate import seat_block_on_online, seat_exceeded


class _St:
    def __init__(self, enforce, seats):
        self.enforce = enforce
        self.seats = seats


# ── seat_exceeded（既有原语，补强回归）──────────────────────────────────────

def test_seat_exceeded_enforce_off_never_blocks():
    assert seat_exceeded(_St(False, 2), 99) is False

def test_seat_exceeded_unlimited_seats():
    assert seat_exceeded(_St(True, 0), 99) is False

def test_seat_exceeded_over_limit():
    assert seat_exceeded(_St(True, 2), 3) is True
    assert seat_exceeded(_St(True, 2), 2) is False


# ── seat_block_on_online（新增，上线边界判定）────────────────────────────────

def test_block_new_agent_when_full():
    # 已 2 人在线，seats=2，新坐席 a3 上线 → prospective=3 > 2 → 拦截
    st = _St(True, 2)
    assert seat_block_on_online(st, ["a1", "a2"], "a3") is True

def test_existing_online_agent_not_kicked():
    # a1 已在线，重复 set/heartbeat：others={a2}, prospective=2 <= 2 → 放行
    st = _St(True, 2)
    assert seat_block_on_online(st, ["a1", "a2"], "a1") is False

def test_under_limit_allows_new_agent():
    st = _St(True, 3)
    assert seat_block_on_online(st, ["a1", "a2"], "a3") is False

def test_enforce_off_allows_everything():
    st = _St(False, 1)
    assert seat_block_on_online(st, ["a1", "a2", "a3"], "a9") is False

def test_dedup_online_ids():
    # 同一坐席多端在线去重：others 去重后 {a2}, prospective=2 <= 2
    st = _St(True, 2)
    assert seat_block_on_online(st, ["a2", "a2", "a1"], "a1") is False
