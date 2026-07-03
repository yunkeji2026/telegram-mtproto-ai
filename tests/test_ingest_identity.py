"""跨平台入站身份归一回归（WhatsApp/Messenger 等经 HTTP ingest 的号）。

三块：
- ``enrich_ingest_identity``（纯函数）：来显名/通讯录名/裸 id 三态分类 + WhatsApp 私聊
  chat_key 即 E.164 号 → 补 phone；群聊/他平台不补。
- store 集成：``ingest_incoming(..., phone=...)`` 把号码落进资料面板字段，且 no-clobber
  （真名不被随后来的裸号码冲掉、phone 非空不回退）。
- ``PeerIdentityStats.record_ingest``：按平台 × {named,backfilled,raw} 计数 + dump/prom/reset/上限。

纯逻辑（纯函数 / 本地 store / 计数单例），无网络、无 Node。
"""

import pytest

from src.integrations.protocol_bridge import enrich_ingest_identity, ingest_incoming
from src.inbox.store import InboxStore
from src.web.peer_identity_stats import PeerIdentityStats, get_peer_identity_stats


@pytest.fixture(autouse=True)
def _reset_stats():
    get_peer_identity_stats().reset()
    yield
    get_peer_identity_stats().reset()


# ─────────────────── enrich_ingest_identity（纯函数） ───────────────────

def test_enrich_named_real_name_wins_and_wa_phone_derived():
    r = enrich_ingest_identity("whatsapp", "8613800138000", "Alice")
    assert r["display_name"] == "Alice"
    assert r["outcome"] == "named"
    assert r["phone"] == "8613800138000"          # WA 私聊 chat_key 即裸号


def test_enrich_backfilled_from_contact_when_name_missing():
    r = enrich_ingest_identity("whatsapp", "8613800138000", "", contact_name="Bob")
    assert r["display_name"] == "Bob"
    assert r["outcome"] == "backfilled"
    assert r["phone"] == "8613800138000"


def test_enrich_backfilled_when_name_is_bare_key():
    r = enrich_ingest_identity("whatsapp", "639111", "639111", contact_name="Carol")
    assert r["display_name"] == "Carol"            # 来名==裸号 → 视为无名，用通讯录
    assert r["outcome"] == "backfilled"


def test_enrich_raw_when_no_name_no_contact():
    r = enrich_ingest_identity("whatsapp", "639111", "")
    assert r["display_name"] == ""                 # 交调用方回落裸 chat_key
    assert r["outcome"] == "raw"
    assert r["phone"] == "639111"                   # 仍补号（资料面板可显）


def test_enrich_wa_group_no_phone():
    r = enrich_ingest_identity("whatsapp", "120363888", "工作群", chat_type="group")
    assert r["outcome"] == "named"
    assert r["phone"] == ""                          # 群不是号码


def test_enrich_non_whatsapp_no_phone():
    r = enrich_ingest_identity("messenger", "1002233", "Dave")
    assert r["display_name"] == "Dave" and r["outcome"] == "named"
    assert r["phone"] == ""                          # 仅 WhatsApp 派生 phone


def test_enrich_wa_non_numeric_key_no_phone():
    r = enrich_ingest_identity("whatsapp", "vanity-handle", "Eve")
    assert r["phone"] == ""                          # 非纯数字 → 不当号码


def test_enrich_contact_equal_key_is_still_raw():
    r = enrich_ingest_identity("messenger", "1002233", "", contact_name="1002233")
    assert r["display_name"] == "" and r["outcome"] == "raw"   # 通讯录名也是裸 id → raw


# ─────────────────── store 集成（phone 落库 + no-clobber） ───────────────────

def test_ingest_populates_phone_and_no_clobber(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    ck = "8613800138000"
    cid = "whatsapp:acct1:" + ck
    # ① 首条：无名 + 派生 phone → 显名回落裸号，phone 落库
    ingest_incoming(store, platform="whatsapp", account_id="acct1", chat_key=ck,
                    name="", text="hi", ts=10, msg_id="m1", direction="in", phone=ck)
    row = store.get_conversation(cid)
    assert row["phone"] == ck
    assert row["display_name"] == ck               # 无名 → 回落裸号
    # ② 后续拿到真名 → 覆盖显名，phone 不变
    ingest_incoming(store, platform="whatsapp", account_id="acct1", chat_key=ck,
                    name="Alice", text="hi2", ts=20, msg_id="m2", direction="in", phone=ck)
    row = store.get_conversation(cid)
    assert row["display_name"] == "Alice"
    assert row["phone"] == ck
    # ③ 再来一条裸号码显名（== chat_key）→ 真名不被冲掉，phone 不回退
    ingest_incoming(store, platform="whatsapp", account_id="acct1", chat_key=ck,
                    name=ck, text="hi3", ts=30, msg_id="m3", direction="in", phone=ck)
    row = store.get_conversation(cid)
    assert row["display_name"] == "Alice"          # no-clobber
    assert row["phone"] == ck
    store.close()


# ─────────────────── PeerIdentityStats.record_ingest ───────────────────

def test_record_ingest_aggregate_and_by_platform():
    s = PeerIdentityStats()
    s.record_ingest("whatsapp", "named")
    s.record_ingest("whatsapp", "raw")
    s.record_ingest("messenger", "backfilled")
    s.record_ingest("whatsapp", "bogus")     # 未知 outcome → 忽略
    s.record_ingest("", "named")              # 空平台 → 忽略
    ing = s.dump()["ingest"]
    assert ing["total"] == 3
    assert ing["named"] == 1 and ing["backfilled"] == 1 and ing["raw"] == 1
    assert ing["by_platform"]["whatsapp"] == {"named": 1, "backfilled": 0, "raw": 1}
    assert ing["by_platform"]["messenger"]["backfilled"] == 1
    assert "" not in ing["by_platform"]


def test_record_ingest_platform_bounded(monkeypatch):
    import src.web.peer_identity_stats as PS
    monkeypatch.setattr(PS, "_INGEST_PLAT_MAX", 2)
    s = PeerIdentityStats()
    for i in range(6):
        s.record_ingest(f"plat{i}", "raw")
    ing = s.dump()["ingest"]
    assert len(ing["by_platform"]) == 2       # distinct 平台封顶
    assert ing["raw"] == 2                     # 超上限的新平台整条丢弃（平台本有限，防脏输入撑爆）


def test_record_ingest_prom_and_reset():
    s = PeerIdentityStats()
    s.record_ingest("whatsapp", "named")
    s.record_ingest("messenger", "raw")
    out = s.dump_prom()
    assert "# TYPE peer_identity_ingest_total counter" in out
    assert 'peer_identity_ingest_total{platform="whatsapp",outcome="named"} 1' in out
    assert 'peer_identity_ingest_total{platform="messenger",outcome="raw"} 1' in out
    s.reset()
    assert s.dump()["ingest"]["total"] == 0 and s.dump()["ingest"]["by_platform"] == {}


# ─────────────────── PeerIdentityStats.record_avatar（头像代理命中）───────────────────

def test_record_avatar_aggregate_and_by_platform():
    s = PeerIdentityStats()
    s.record_avatar("messenger", "fetched")
    s.record_avatar("messenger", "empty")
    s.record_avatar("whatsapp", "cache_hit")
    s.record_avatar("whatsapp", "neg_hit")
    s.record_avatar("messenger", "bogus")    # 未知 outcome → 忽略
    s.record_avatar("", "fetched")            # 空平台 → 忽略
    av = s.dump()["avatar"]
    assert av["total"] == 4
    assert av["fetched"] == 1 and av["empty"] == 1
    assert av["cache_hit"] == 1 and av["neg_hit"] == 1 and av["error"] == 0
    assert av["by_platform"]["messenger"]["fetched"] == 1
    assert av["by_platform"]["messenger"]["empty"] == 1
    assert av["by_platform"]["whatsapp"]["cache_hit"] == 1
    assert "" not in av["by_platform"]


def test_record_avatar_platform_bounded(monkeypatch):
    import src.web.peer_identity_stats as PS
    monkeypatch.setattr(PS, "_AVATAR_PLAT_MAX", 2)
    s = PeerIdentityStats()
    for i in range(6):
        s.record_avatar(f"plat{i}", "empty")
    av = s.dump()["avatar"]
    assert len(av["by_platform"]) == 2        # distinct 平台封顶
    assert av["empty"] == 2                    # 超上限的新平台整条丢弃


def test_record_avatar_prom_and_reset():
    s = PeerIdentityStats()
    s.record_avatar("messenger", "fetched")
    s.record_avatar("messenger", "empty")
    out = s.dump_prom()
    assert "# TYPE peer_identity_avatar_total counter" in out
    assert 'peer_identity_avatar_total{platform="messenger",outcome="fetched"} 1' in out
    assert 'peer_identity_avatar_total{platform="messenger",outcome="empty"} 1' in out
    s.reset()
    assert s.dump()["avatar"]["total"] == 0 and s.dump()["avatar"]["by_platform"] == {}


# ─────────────────── PeerIdentityStats.record_panel（资料面板就绪度，F3）───────────────────

def test_record_panel_aggregate_and_by_platform():
    s = PeerIdentityStats()
    s.record_panel("telegram", has_name=True, has_username=True, has_phone=False)
    s.record_panel("telegram", has_name=False)                 # 打开了但仍是数字号
    s.record_panel("line", has_name=True, has_phone=True)
    s.record_panel("", has_name=True)                          # 空平台 → 忽略
    pn = s.dump()["panel"]
    assert pn["opens"] == 3                                     # 3 次有效打开
    assert pn["name"] == 2 and pn["username"] == 1 and pn["phone"] == 1
    assert pn["by_platform"]["telegram"]["opens"] == 2
    assert pn["by_platform"]["telegram"]["name"] == 1          # 2 次里只 1 次有真名
    assert pn["by_platform"]["line"]["phone"] == 1
    assert "" not in pn["by_platform"]


def test_record_panel_platform_bounded(monkeypatch):
    import src.web.peer_identity_stats as PS
    monkeypatch.setattr(PS, "_PANEL_PLAT_MAX", 2)
    s = PeerIdentityStats()
    for i in range(6):
        s.record_panel(f"plat{i}", has_name=True)
    pn = s.dump()["panel"]
    assert len(pn["by_platform"]) == 2                         # distinct 平台封顶
    assert pn["opens"] == 2                                     # 超上限的新平台整条丢弃


def test_record_panel_prom_and_reset():
    s = PeerIdentityStats()
    s.record_panel("telegram", has_name=True, has_username=True)
    out = s.dump_prom()
    assert "# TYPE peer_identity_panel_total counter" in out
    assert 'peer_identity_panel_total{platform="telegram",field="opens"} 1' in out
    assert 'peer_identity_panel_total{platform="telegram",field="name"} 1' in out
    assert 'peer_identity_panel_total{platform="telegram",field="phone"} 0' in out
    s.reset()
    assert s.dump()["panel"]["opens"] == 0 and s.dump()["panel"]["by_platform"] == {}


# ─────────────────── 读路由 panel helper：真名判定 + 去重记录 ───────────────────

def test_name_is_real_classification():
    from src.web.routes.unified_inbox_read_routes import _name_is_real
    assert _name_is_real("林小雨", "123456") is True
    assert _name_is_real("", "123456") is False               # 空 → 非真名
    assert _name_is_real("123456", "123456") is False         # 等于裸 chat_key → 非真名
    assert _name_is_real("889900", "u_abc") is False          # 纯数字（TG 数字号）→ 非真名
    assert _name_is_real("Ann", "u_abc") is True


def test_record_panel_identity_dedups_by_conversation():
    import src.web.routes.unified_inbox_read_routes as RR
    from src.web.peer_identity_stats import get_peer_identity_stats
    get_peer_identity_stats().reset()
    RR._PANEL_SEEN.clear()
    chat = {"conversation_id": "telegram:default:999", "platform": "telegram",
            "name": "889900", "chat_key": "889900", "username": "", "phone": ""}
    RR._record_panel_identity(chat)
    RR._record_panel_identity(chat)                            # 同会话再开 → 去重不重复计
    RR._record_panel_identity(dict(chat, conversation_id="telegram:default:1000",
                                    name="Bob", username="bob"))
    pn = get_peer_identity_stats().dump()["panel"]
    assert pn["opens"] == 2                                     # 两个 distinct 会话
    assert pn["name"] == 1 and pn["username"] == 1             # 第二个有真名+username
    get_peer_identity_stats().reset()
    RR._PANEL_SEEN.clear()


# ─────────────────── PeerIdentityStats.record_readback（实时列表回读补齐，F5）───────────────────

def test_record_readback_aggregate_and_by_platform():
    s = PeerIdentityStats()
    s.record_readback("line", ["name", "avatar"])
    s.record_readback("line", ["username"])
    s.record_readback("whatsapp", ["phone", "avatar"])
    s.record_readback("", ["name"])                            # 空平台 → 忽略
    rb = s.dump()["readback"]
    assert rb["rows"] == 3                                      # 3 次有效补齐（去重由调用方保证）
    assert rb["name"] == 1 and rb["username"] == 1
    assert rb["phone"] == 1 and rb["avatar"] == 2
    assert rb["by_platform"]["line"]["rows"] == 2
    assert rb["by_platform"]["line"]["name"] == 1
    assert rb["by_platform"]["whatsapp"]["avatar"] == 1
    assert "" not in rb["by_platform"]


def test_record_readback_unknown_fields_ignored():
    s = PeerIdentityStats()
    s.record_readback("line", ["bogus"])                       # 全非法字段 → 整条忽略（不计 rows）
    s.record_readback("line", ["name", "bogus"])               # 混入合法 → 只计 name + rows
    rb = s.dump()["readback"]
    assert rb["rows"] == 1 and rb["name"] == 1
    assert rb["by_platform"]["line"]["rows"] == 1


def test_record_readback_platform_bounded(monkeypatch):
    import src.web.peer_identity_stats as PS
    monkeypatch.setattr(PS, "_READBACK_PLAT_MAX", 2)
    s = PeerIdentityStats()
    for i in range(6):
        s.record_readback(f"plat{i}", ["name"])
    rb = s.dump()["readback"]
    assert len(rb["by_platform"]) == 2                         # distinct 平台封顶
    assert rb["rows"] == 2                                      # 超上限的新平台整条丢弃


def test_record_readback_prom_and_reset():
    s = PeerIdentityStats()
    s.record_readback("line", ["name", "avatar"])
    out = s.dump_prom()
    assert "# TYPE peer_identity_readback_total counter" in out
    assert 'peer_identity_readback_total{platform="line",field="rows"} 1' in out
    assert 'peer_identity_readback_total{platform="line",field="name"} 1' in out
    assert 'peer_identity_readback_total{platform="line",field="avatar"} 1' in out
    assert 'peer_identity_readback_total{platform="line",field="phone"} 0' in out
    s.reset()
    assert s.dump()["readback"]["rows"] == 0 and s.dump()["readback"]["by_platform"] == {}
