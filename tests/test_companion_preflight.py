"""Phase N：真号扫码陪聊上线前自检纯函数单测。

覆盖：未启用→不适用 / 凭证缺失→fail / 编排器关→fail / protocol 关→fail /
全开+闸门关→yellow / 全绿 / 代理 warn。
"""
from src.ops.companion_preflight import build_companion_preflight


def _cfg(**overrides):
    base = {
        "telegram": {"api_id": 123456, "api_hash": "abcdef0123"},
        "platform_login": {
            "orchestrator_enabled": True,
            "telegram": {"protocol_enabled": True, "companion_runtime": True},
        },
        "companion_send_gate": {"enabled": True},
        "proxy_pool": ["socks5://1.2.3.4:1080"],
    }
    base.update(overrides)
    return base


def _by_id(res):
    return {c["id"]: c for c in res["checks"]}


def test_not_applicable_when_runtime_off():
    cfg = _cfg(platform_login={"orchestrator_enabled": False,
                               "telegram": {"protocol_enabled": False,
                                            "companion_runtime": False}})
    res = build_companion_preflight(cfg)
    assert res["applicable"] is False
    assert res["ready"] is True and res["light"] == "green"
    assert len(res["checks"]) == 1 and res["checks"][0]["id"] == "companion_runtime"


def test_all_green():
    res = build_companion_preflight(_cfg())
    assert res["applicable"] is True
    assert res["light"] == "green" and res["ready"] is True
    assert res["summary"]["fail"] == 0 and res["summary"]["warn"] == 0


def test_missing_credentials_fail():
    res = build_companion_preflight(_cfg(telegram={"api_id": "", "api_hash": ""}))
    assert _by_id(res)["tg_credentials"]["status"] == "fail"
    assert res["light"] == "red" and res["ready"] is False


def test_placeholder_credentials_fail():
    res = build_companion_preflight(
        _cfg(telegram={"api_id": "your_api_id", "api_hash": "<hash>"}))
    assert _by_id(res)["tg_credentials"]["status"] == "fail"


def test_orchestrator_off_fail():
    res = build_companion_preflight(_cfg(platform_login={
        "orchestrator_enabled": False,
        "telegram": {"protocol_enabled": True, "companion_runtime": True}}))
    assert _by_id(res)["orchestrator"]["status"] == "fail"
    assert res["ready"] is False


def test_protocol_off_fail():
    res = build_companion_preflight(_cfg(platform_login={
        "orchestrator_enabled": True,
        "telegram": {"protocol_enabled": False, "companion_runtime": True}}))
    assert _by_id(res)["protocol"]["status"] == "fail"


def test_send_gate_off_is_warn_not_fail():
    res = build_companion_preflight(_cfg(companion_send_gate={"enabled": False}))
    assert _by_id(res)["send_gate"]["status"] == "warn"
    assert res["light"] == "yellow" and res["ready"] is True  # warn 不拦上线


def test_no_proxy_is_warn():
    cfg = _cfg(proxy_pool=[])
    res = build_companion_preflight(cfg)
    assert _by_id(res)["proxy"]["status"] == "warn"


def test_empty_config_not_applicable():
    res = build_companion_preflight({})
    assert res["applicable"] is False and res["ready"] is True
