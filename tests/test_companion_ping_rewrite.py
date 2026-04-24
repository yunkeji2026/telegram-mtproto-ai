"""conversion 域：短寒暄时剥除「有什么可以帮」类客服套话。"""


def _mixin():
    class T:
        config = type(
            "Cfg",
            (),
            {
                "get": lambda self, k, d=None: d,
                "config": {"domain": "conversion"},
            },
        )()

    from src.client.sender import TelegramSenderMixin

    class M(T, TelegramSenderMixin):
        pass

    return M()


def test_rewrite_ping_replaces_cs():
    m = _mixin()
    out = m._rewrite_companion_helpdesk_ping("在的，有什么可以帮您的？", "在吗")
    assert "有什么可以帮" not in out
    assert len(out) < 48


def test_rewrite_standalone_zai():
    m = _mixin()
    out = m._rewrite_companion_helpdesk_ping("在的，有什么可以帮您的？", "在")
    assert "有什么可以帮" not in out


def test_non_greeting_long_untouched():
    m = _mixin()
    raw = "在的，有什么可以帮您的？"
    out = m._rewrite_companion_helpdesk_ping(raw, "订单为什么还不到账")
    assert out == raw

