"""Phase C：RPA 发送守卫单测 —— 让 G1 Kill-Switch 真·覆盖三端 RPA。

runner 本体需真机才能 E2E，这里单测共用守卫 `rpa_send_blocked` 的判定逻辑：
global/platform/account 三级作用域对 line/messenger/whatsapp 均生效，且故障放行不阻断。
"""
import pytest

from src.ops.kill_switch import KillSwitch
from src.integrations.shared.rpa_send_guard import rpa_send_blocked


def test_not_blocked_when_clean(tmp_path):
    ks = KillSwitch(tmp_path / "rf.db")
    for plat in ("line", "messenger", "whatsapp"):
        assert rpa_send_blocked(plat, "1", kill_switch=ks) == (False, "")


def test_global_freeze_covers_all_three_rpa(tmp_path):
    ks = KillSwitch(tmp_path / "rf.db")
    ks.set("global", reason="emergency")
    for plat in ("line", "messenger", "whatsapp"):
        blocked, scope = rpa_send_blocked(plat, "any", kill_switch=ks)
        assert blocked is True and scope == "global"


def test_platform_scope_only_affects_that_platform(tmp_path):
    ks = KillSwitch(tmp_path / "rf.db")
    ks.set("platform:line")
    assert rpa_send_blocked("line", "1", kill_switch=ks)[0] is True
    assert rpa_send_blocked("messenger", "1", kill_switch=ks)[0] is False


def test_account_scope(tmp_path):
    ks = KillSwitch(tmp_path / "rf.db")
    ks.set("account:whatsapp:888")
    assert rpa_send_blocked("whatsapp", "888", kill_switch=ks)[0] is True
    assert rpa_send_blocked("whatsapp", "999", kill_switch=ks)[0] is False


def test_guard_never_raises_on_broken_killswitch():
    class _Broken:
        def is_blocked(self, *a, **k):
            raise RuntimeError("db down")

    # 守卫故障 → 放行（不得阻断/掩盖正常 RPA 发送）
    assert rpa_send_blocked("line", "1", kill_switch=_Broken()) == (False, "")


def test_default_account_id_fallback(tmp_path):
    ks = KillSwitch(tmp_path / "rf.db")
    ks.set("account:line:default")
    assert rpa_send_blocked("line", None, kill_switch=ks)[0] is True
