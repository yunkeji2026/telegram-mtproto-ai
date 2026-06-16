"""C0-1 离线授权（License）核心测试：签发 / 验签 / 状态机 / 防篡改。"""

import time

import pytest

from src.licensing import (
    LicenseManager,
    generate_keypair,
    issue_license,
)


@pytest.fixture()
def keypair():
    return generate_keypair()


def _mk_manager(token, pub_hex, *, now=None):
    return LicenseManager(
        license_token=token,
        public_key_hex=pub_hex,
        now_fn=(now or time.time),
    )


def test_issue_and_verify_active(keypair):
    """有效未过期授权 → state=active，功能位/渠道正确解析。"""
    exp = int(time.time()) + 30 * 86400
    token = issue_license(
        {"sub": "ACME", "plan": "pro", "exp": exp, "seats": 10,
         "channels": ["telegram", "line", "web"],
         "features": {"l4": True, "white_label": True}},
        keypair["private_hex"],
    )
    st = _mk_manager(token, keypair["public_hex"]).status()
    assert st.state == "active"
    assert st.licensed is True
    assert st.plan == "pro"
    assert st.customer == "ACME"
    assert st.seats == 10
    assert st.feature_enabled("l4") is True
    assert st.feature_enabled("white_label") is True
    assert st.feature_enabled("nonexistent") is False
    assert st.channel_allowed("line") is True
    assert st.channel_allowed("messenger") is False
    assert 0 <= st.days_left <= 30


def test_perpetual_license_has_no_expiry(keypair):
    """exp 省略 = 永久授权，days_left=None。"""
    token = issue_license({"sub": "X", "plan": "flagship"}, keypair["private_hex"])
    st = _mk_manager(token, keypair["public_hex"]).status()
    assert st.state == "active"
    assert st.expires_at == 0
    assert st.days_left is None


def test_grace_period_after_expiry(keypair):
    """过期但在宽限期内 → state=grace，仍 licensed。"""
    now = time.time()
    exp = int(now) - 2 * 86400  # 2 天前过期
    token = issue_license(
        {"sub": "X", "plan": "basic", "exp": exp, "grace_days": 7},
        keypair["private_hex"],
    )
    st = _mk_manager(token, keypair["public_hex"], now=lambda: now).status()
    assert st.state == "grace"
    assert st.licensed is True
    assert any("宽限" in m for m in st.messages)


def test_expired_beyond_grace(keypair):
    """过期超宽限 → state=expired，不再 licensed；功能位关闭。"""
    now = time.time()
    exp = int(now) - 30 * 86400
    token = issue_license(
        {"sub": "X", "plan": "pro", "exp": exp, "grace_days": 7,
         "features": {"l4": True}},
        keypair["private_hex"],
    )
    st = _mk_manager(token, keypair["public_hex"], now=lambda: now).status()
    assert st.state == "expired"
    assert st.licensed is False
    assert st.feature_enabled("l4") is False
    # C0-1：即使过期也不强制只读（gating 留给 C0-3）
    assert st.read_only is False


def test_unlicensed_is_community_mode():
    """无授权文件 → state=unlicensed（社区模式），不崩。"""
    st = LicenseManager(license_token=None, license_path="/no/such/file").status()
    assert st.state == "unlicensed"
    assert st.plan == "community"
    assert st.licensed is False


def test_tampered_payload_rejected(keypair):
    """篡改 payload → 签名不匹配 → state=invalid。"""
    token = issue_license(
        {"sub": "X", "plan": "basic", "seats": 1}, keypair["private_hex"],
    )
    body_b64, sig_b64 = token.split(".", 1)
    import base64
    import json
    pad = "=" * (-len(body_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(body_b64 + pad))
    payload["seats"] = 9999  # 偷偷提升席位
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    forged_body = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    forged = f"{forged_body}.{sig_b64}"
    st = _mk_manager(forged, keypair["public_hex"]).status()
    assert st.state == "invalid"
    assert st.licensed is False


def test_wrong_public_key_rejected(keypair):
    """用另一对密钥的公钥验签 → invalid（防他人私钥伪造）。"""
    other = generate_keypair()
    token = issue_license({"sub": "X", "plan": "pro"}, keypair["private_hex"])
    st = _mk_manager(token, other["public_hex"]).status()
    assert st.state == "invalid"


def test_status_to_dict_shape(keypair):
    token = issue_license({"sub": "X", "plan": "pro"}, keypair["private_hex"])
    d = _mk_manager(token, keypair["public_hex"]).status().to_dict()
    for k in ("state", "licensed", "plan", "seats", "channels",
              "features", "read_only", "messages"):
        assert k in d


def test_license_routes_registered():
    """只读状态端点随 register_license_routes 挂载。"""
    import inspect
    from src.web.routes import license_routes
    src = inspect.getsource(license_routes.register_license_routes)
    assert "/api/admin/license" in src
    assert "/api/admin/license/reload" in src


# ── C0-3 套餐 gating ─────────────────────────────────────────────────────────

def _expired_status(keypair, enforce):
    now = time.time()
    exp = int(now) - 30 * 86400
    token = issue_license(
        {"sub": "X", "plan": "pro", "exp": exp, "grace_days": 7,
         "channels": ["telegram"], "features": {"l4": True}},
        keypair["private_hex"],
    )
    return LicenseManager(
        license_token=token, public_key_hex=keypair["public_hex"],
        enforce=enforce, now_fn=lambda: now,
    ).status()


def test_read_only_only_when_enforce_and_expired(keypair):
    """read_only 仅在 enforce=True 且 expired/invalid 时为真；enforce 关恒 False。"""
    assert _expired_status(keypair, enforce=False).read_only is False
    assert _expired_status(keypair, enforce=True).read_only is True


def test_grace_never_readonly_even_when_enforce(keypair):
    """宽限期内即使 enforce 也不只读（不误伤诚实客户）。"""
    now = time.time()
    exp = int(now) - 2 * 86400
    token = issue_license(
        {"sub": "X", "plan": "basic", "exp": exp, "grace_days": 7},
        keypair["private_hex"],
    )
    st = LicenseManager(
        license_token=token, public_key_hex=keypair["public_hex"],
        enforce=True, now_fn=lambda: now,
    ).status()
    assert st.state == "grace"
    assert st.read_only is False


def test_unlicensed_never_readonly_even_when_enforce():
    """社区模式（无授权）即使 enforce 也不只读（否则会 brick 社区/开发部署）。"""
    st = LicenseManager(
        license_token=None, license_path="/no/such", enforce=True,
    ).status()
    assert st.state == "unlicensed"
    assert st.read_only is False


def test_is_write_blocked_respects_allowlist(keypair):
    """只读模式下写被拦，但放行白名单（登录/授权/心跳）+ 读请求放行。"""
    from src.licensing import is_write_blocked
    st = _expired_status(keypair, enforce=True)
    assert is_write_blocked("/api/workspace/send", "POST", st) is True
    assert is_write_blocked("/api/workspace/send", "GET", st) is False
    assert is_write_blocked("/logout", "POST", st) is False
    assert is_write_blocked("/api/admin/license/reload", "POST", st) is False
    assert is_write_blocked("/api/workspace/heartbeat", "POST", st) is False


def test_is_write_blocked_off_when_not_readonly(keypair):
    """enforce 关时一律不拦（现网零破坏）。"""
    from src.licensing import is_write_blocked
    st = _expired_status(keypair, enforce=False)
    assert is_write_blocked("/api/workspace/send", "POST", st) is False


def test_feature_and_channel_gating(keypair):
    """enforce 开时功能位/渠道按授权放行；enforce 关恒放行。"""
    from src.licensing import channel_allowed, feature_allowed
    now = time.time()
    token = issue_license(
        {"sub": "X", "plan": "pro", "exp": int(now) + 86400,
         "channels": ["telegram"], "features": {"l4": True}},
        keypair["private_hex"],
    )
    on = LicenseManager(license_token=token, public_key_hex=keypair["public_hex"],
                        enforce=True, now_fn=lambda: now).status()
    assert feature_allowed(on, "l4") is True
    assert feature_allowed(on, "white_label") is False
    assert channel_allowed(on, "telegram") is True
    assert channel_allowed(on, "line") is False
    off = LicenseManager(license_token=token, public_key_hex=keypair["public_hex"],
                         enforce=False, now_fn=lambda: now).status()
    assert feature_allowed(off, "white_label") is True
    assert channel_allowed(off, "line") is True


def test_seat_exceeded(keypair):
    """席位超额：enforce 开 + seats>0 时活跃>席位为超额；seats=0 不限。"""
    from src.licensing import seat_exceeded
    now = time.time()

    def _st(seats, enforce):
        tok = issue_license(
            {"sub": "X", "plan": "pro", "exp": int(now) + 86400, "seats": seats},
            keypair["private_hex"])
        return LicenseManager(license_token=tok, public_key_hex=keypair["public_hex"],
                              enforce=enforce, now_fn=lambda: now).status()
    assert seat_exceeded(_st(3, True), 4) is True
    assert seat_exceeded(_st(3, True), 3) is False
    assert seat_exceeded(_st(0, True), 999) is False   # 不限
    assert seat_exceeded(_st(3, False), 4) is False     # enforce 关


def test_configure_license_manager_sets_enforce():
    """configure_license_manager 就地更新单例强制开关。"""
    from src.licensing import (
        configure_license_manager,
        get_license_manager,
        reset_license_manager,
    )
    reset_license_manager()
    try:
        st = configure_license_manager(enforce=True)
        assert st.enforce is True
        assert get_license_manager().status().enforce is True
    finally:
        reset_license_manager()


def test_setup_channel_gating_in_source():
    """C0-3：渠道接入端点含套餐 gating（channel_allowed 检查）。"""
    import inspect
    from src.web.routes import unified_inbox_setup_routes as mod
    src = inspect.getsource(mod.register_setup_routes)
    assert "channel_allowed" in src
    assert "channel_not_licensed" in src


def test_readonly_middleware_wired_in_admin():
    """C0-3：管理 app 创建处挂了只读守卫 middleware。"""
    import inspect
    from src.web import admin
    src = inspect.getsource(admin.create_app)
    assert "is_write_blocked" in src
    assert "license_readonly" in src
