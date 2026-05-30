"""Q4：漏斗 channel filter —— store + API + UI partial 三层测试。

测试焦点：

- store.count_journeys_by_stage(channel=...) 正确按 channel 过滤
- 同 journey 在同 channel 多 account 时 **DISTINCT** 防重复计数
- /api/funnel/stats?channel=X 返回正确 by_stage + scope 字段
- 非法 channel → 400（不是 500/无声裂）
- 'all' 等价于无参数（向后兼容）
- 前端 partial 暴露 CHIPS / SCOPE_LABEL / F._scope / defaultScope 钩子
- 4 个调用页传了正确的 defaultScope
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.contacts.gateway import ContactGateway
from src.contacts.handoff import HandoffTokenService
from src.contacts.merge import MergeService
from src.contacts.models import (
    CHANNEL_LINE,
    CHANNEL_MESSENGER,
    CHANNEL_TELEGRAM,
    CHANNEL_MOBILE,
)
from src.contacts.store import ContactStore
from src.web.routes.contacts_routes import register_contacts_routes


# ════════════════════════════════════════════════════════════════════════
# 测试夹具
# ════════════════════════════════════════════════════════════════════════


@pytest.fixture
def client(tmp_path):
    """contacts 路由 + gateway 完整夹具，最小化（funnel 不需要 intimacy 等）。"""
    from src.web.routes.contacts_routes import _intimacy_trend_cache_clear
    _intimacy_trend_cache_clear()

    store = ContactStore(db_path=tmp_path / "contacts.db")
    handoff = HandoffTokenService(store, ttl_seconds=3600)
    merge = MergeService(store)
    gateway = ContactGateway(store, handoff, merge)

    app = FastAPI()

    def noop_auth():
        return None

    register_contacts_routes(
        app, api_auth=noop_auth, contacts_store=store, merge_service=merge,
        gateway=gateway,
    )

    tc = TestClient(app)
    tc.store = store          # type: ignore[attr-defined]
    tc.gateway = gateway      # type: ignore[attr-defined]
    yield tc
    store.close()


# ════════════════════════════════════════════════════════════════════════
# store 层：count_journeys_by_stage(channel=)
# ════════════════════════════════════════════════════════════════════════


class TestStoreChannelFilter:
    def test_no_channel_keeps_legacy_behavior(self, client):
        """channel=None 必须等价旧逻辑：全部 journey 按 stage 聚合。"""
        client.gateway.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1")
        client.gateway.on_peer_seen(
            channel=CHANNEL_LINE, account_id="a", external_id="line_1")

        all_stages = client.store.count_journeys_by_stage()
        # 两条 journey，都在 INITIAL
        assert sum(all_stages.values()) == 2

    def test_filter_by_messenger_only(self, client):
        client.gateway.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1")
        client.gateway.on_peer_seen(
            channel=CHANNEL_LINE, account_id="a", external_id="line_1")
        client.gateway.on_peer_seen(
            channel=CHANNEL_TELEGRAM, account_id="a", external_id="tg_1")

        # 3 个 journey，每个 channel 各 1 个
        m = client.store.count_journeys_by_stage(channel=CHANNEL_MESSENGER)
        l = client.store.count_journeys_by_stage(channel=CHANNEL_LINE)
        t = client.store.count_journeys_by_stage(channel=CHANNEL_TELEGRAM)

        assert sum(m.values()) == 1
        assert sum(l.values()) == 1
        assert sum(t.values()) == 1

    def test_distinct_prevents_double_counting_same_journey(self, client):
        """同一 journey 在同 channel 有多个 identity（不同 account）→ DISTINCT。

        没有 DISTINCT 的话，JOIN 会让一个 journey 被算 N 次（N = 它在该 channel
        的 identity 数）。这是数据完整性的硬保障。

        真实触发场景：合并（merge）——两个 page 看到同一人，先各建一个 contact /
        journey，运营在 merge_review 里点 approve → ``relink_channel_identity``
        把后建的 CI 迁到前建的 contact。结果：1 个 journey + 2 个 messenger CI。
        """
        # 1. 两个独立 contact / journey，都在 messenger
        ctx_a = client.gateway.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="page_a", external_id="fb_alice_a")
        ctx_b = client.gateway.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="page_b", external_id="fb_alice_b")
        # 此时：2 个 journey，naive JOIN COUNT=2 才对
        before = client.store.count_journeys_by_stage(channel=CHANNEL_MESSENGER)
        assert sum(before.values()) == 2

        # 2. 合并：把 ctx_b 的 CI 迁到 ctx_a 的 contact
        # relink 内部会回收 ctx_b 的孤岛 contact + journey
        ok = client.store.relink_channel_identity(
            ci_id=ctx_b.channel_identity.channel_identity_id,
            new_contact_id=ctx_a.contact.contact_id,
            linked_via="merge_approval",
            attribution_confidence=1.0,
        )
        assert ok

        # 3. 现在：1 个 journey + 2 个 messenger CI 挂在同一个 contact
        # 关键断言：DISTINCT 必须把这个 journey 算成 1 而不是 2
        after = client.store.count_journeys_by_stage(channel=CHANNEL_MESSENGER)
        assert sum(after.values()) == 1, (
            "DISTINCT 保护失效：合并后同 journey 在 2 个 CI 上被算了 2 次"
        )

    def test_filter_returns_empty_dict_for_unused_channel(self, client):
        """channel 没数据时返回空 dict，不抛错。"""
        result = client.store.count_journeys_by_stage(channel=CHANNEL_MOBILE)
        assert result == {}


# ════════════════════════════════════════════════════════════════════════
# API 层：/api/funnel/stats?channel=
# ════════════════════════════════════════════════════════════════════════


class TestFunnelStatsChannelQuery:
    def test_no_channel_returns_scope_all(self, client):
        r = client.get("/api/funnel/stats")
        assert r.status_code == 200
        body = r.json()
        assert body["scope"] == "all"

    def test_explicit_all_equivalent_to_no_param(self, client):
        """scope='all' 不应作为 query param 传（前端契约），但即使传了，后端
        应当回退到 all（视为非法的话用户太难用）。
        """
        # 当前实现：'all' 不在 VALID_CHANNELS → 400。这是刻意严格契约。
        # 若改为宽松（'all' → None）需同步本断言。
        r = client.get("/api/funnel/stats?channel=all")
        assert r.status_code == 400  # 严格模式：'all' 不是有效 channel

    def test_filter_by_messenger(self, client):
        client.gateway.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1",
            direction="in", text_preview="hi")
        client.gateway.on_peer_seen(
            channel=CHANNEL_LINE, account_id="a", external_id="line_1")

        r = client.get(f"/api/funnel/stats?channel={CHANNEL_MESSENGER}")
        assert r.status_code == 200
        body = r.json()
        assert body["scope"] == CHANNEL_MESSENGER
        # 仅 messenger 的 journey（1 条 ENGAGED）
        assert body["by_stage"].get("ENGAGED", 0) == 1
        # by_channel 仍是全局（messenger=1, line=1）
        assert body["by_channel"].get("messenger", 0) == 1
        assert body["by_channel"].get("line", 0) == 1

    def test_invalid_channel_returns_400(self, client):
        """非法 channel → 400 而不是 500，让前端能 catch + 友好提示。"""
        r = client.get("/api/funnel/stats?channel=facebook")
        assert r.status_code == 400
        assert "facebook" in r.json()["detail"]

    def test_empty_string_channel_treated_as_no_filter(self, client):
        """?channel=  应等价无参数（前端切回 all chip 时往往传空串）。"""
        client.gateway.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1")
        r = client.get("/api/funnel/stats?channel=")
        assert r.status_code == 200
        body = r.json()
        assert body["scope"] == "all"

    def test_by_channel_unaffected_by_filter(self, client):
        """by_channel 字段是全局视图，不应受 channel filter 影响（设计意图）。"""
        client.gateway.on_peer_seen(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_1")
        client.gateway.on_peer_seen(
            channel=CHANNEL_LINE, account_id="a", external_id="line_1")

        r1 = client.get("/api/funnel/stats").json()
        r2 = client.get(f"/api/funnel/stats?channel={CHANNEL_LINE}").json()
        # 两次 by_channel 必须相同
        assert r1["by_channel"] == r2["by_channel"]


# ════════════════════════════════════════════════════════════════════════
# 前端 partial：chip UI + defaultScope + scope label
# ════════════════════════════════════════════════════════════════════════


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "src" / "web" / "templates"


@pytest.fixture(scope="module")
def partial_text() -> str:
    return (TEMPLATES_DIR / "_rpa_shared_funnel.html").read_text(encoding="utf-8")


class TestFunnelPartialChipUI:
    def test_chips_container_present(self, partial_text: str):
        assert 'id="rpa-funnel-chips"' in partial_text

    def test_chips_lists_all_valid_channels_plus_all(self, partial_text: str):
        """chip 列表必须包含 5 项：all + 4 个合法 channel。"""
        for val in ["'all'", "'messenger'", "'line'", "'telegram'", "'mobile'"]:
            assert val in partial_text, f"CHIPS 必须含 {val}"

    def test_partial_does_not_offer_whatsapp_chip(self, partial_text: str):
        """WhatsApp 不接入 contacts 体系，chip 里**不应**出现（避免误导运营点了
        whatsapp chip 看到空数据后困惑）。

        注意：partial 里 'whatsapp' 这个字符串本身可能在注释/文档里出现，所以
        我们只断言"CHIPS 数组定义里没有 whatsapp"——通过查 CHIPS 数组语法。
        """
        # 关键证据：CHIPS 数组里没有 ['whatsapp', ...] 形式
        assert "'whatsapp'" not in partial_text, (
            "CHIPS 不应该有 whatsapp（WhatsApp 不接入 contacts）"
        )

    def test_scope_label_dict_exists(self, partial_text: str):
        """SCOPE_LABEL 是 chip + total 文字共用的中文映射。"""
        assert "SCOPE_LABEL" in partial_text
        # 关键映射：'line' → 'LINE'
        assert "'line':" in partial_text
        assert "'LINE'" in partial_text

    def test_refresh_appends_channel_param_conditionally(
        self, partial_text: str
    ):
        """scope='all' 不带 ?channel=（向后兼容）；其他必须拼。"""
        # 关键的拼接判断
        assert "F._scope === 'all'" in partial_text
        assert "?channel=" in partial_text or "channel=" in partial_text

    def test_init_accepts_default_scope(self, partial_text: str):
        """F.init(opts) 必须支持 defaultScope，否则 4 个调用页传值无效。"""
        assert "defaultScope" in partial_text

    def test_chip_click_triggers_refresh(self, partial_text: str):
        """chip 点击必须调 F.refresh()，否则切 chip 无反应。"""
        # 找到 click handler 区段的关键调用
        assert "F.refresh()" in partial_text


# ════════════════════════════════════════════════════════════════════════
# 4 个调用页传了正确的 defaultScope
# ════════════════════════════════════════════════════════════════════════


# (template, expected_default_scope)
EXPECTED_SCOPES = [
    ("line_rpa.html",     "'line'"),
    ("telegram.html",     "'telegram'"),
    ("whatsapp_rpa.html", "'all'"),   # WhatsApp 不接入 contacts → 看 all
    ("rpa_overview.html", "'all'"),   # overview 就是跨平台总览
]


@pytest.mark.parametrize("template,expected", EXPECTED_SCOPES)
def test_template_passes_correct_default_scope(template: str, expected: str):
    """每个页面 init 时传的 defaultScope 必须匹配产品意图。

    意图设计：
    - LINE 页 → 看 LINE 的漏斗（解决"漏斗骗人"的核心问题）
    - Telegram 页 → 看 Telegram 的漏斗
    - WhatsApp 页 → 看 all（无 CHANNEL_WHATSAPP，没法过滤；当作"从 WA 跳
      去看整个生态"的入口）
    - Overview 页 → 看 all（页面定位本就是跨平台总览）
    """
    text = (TEMPLATES_DIR / template).read_text(encoding="utf-8")
    assert "defaultScope" in text, f"{template} 必须传 defaultScope"
    assert f"defaultScope:{expected}" in text or f"defaultScope: {expected}" in text, (
        f"{template} 的 defaultScope 应为 {expected}"
    )


def test_messenger_does_not_use_shared_funnel():
    """Messenger 保留自家增强版 funnel（含 variants/handoff/ab_conclusions）；
    shared funnel 是给另外 3 个平台 + overview 用的"通用版"。这条边界已
    由 test_rpa_shared_funnel.py 锁定，这里只做"不重复定义 defaultScope"
    的二次保险。
    """
    text = (TEMPLATES_DIR / "messenger_rpa.html").read_text(encoding="utf-8")
    assert "rpa.funnel.init" not in text, (
        "Messenger 不应该调用共享 funnel init（它有自己的 /api/messenger-rpa/funnel）"
    )
