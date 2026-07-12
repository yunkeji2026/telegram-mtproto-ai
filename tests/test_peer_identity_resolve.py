"""惰性 peer 身份解析回归（自愈式补名，覆盖存量 + 未来数字 id 会话）。

两条链路：
- Telegram：``resolve_tg_peer_identity`` 纯核心——坐席打开「一排数字」私聊时按需向 pyrogram
  拉 peer 资料补真实昵称 / @username / 电话，落库走 no-clobber，并进程级正/负缓存防重打 API。
- LINE：``LineProtocolWorker._resolve_peer_name``——私聊入站时按需 getContactsV2 拉发送者显示名
  （备注名优先），per-peer 缓存；查的是对方 mid，天然规避「误标成本账号名」。

均为纯逻辑（fake client / 本地 store / 后台事件循环），无网络、无 Node、无完整 app。
"""

import asyncio
import threading
import types

import pytest

from src.inbox.models import InboxConversation
from src.inbox.store import InboxStore
from src.web.peer_identity_stats import PeerIdentityStats, get_peer_identity_stats
from src.web.routes import unified_inbox_account_routes as R


# ─────────────────────────── 夹具 ───────────────────────────

@pytest.fixture(autouse=True)
def _clear_peer_cache():
    """每个用例前后清空进程级 peer 缓存 + 断网冷却 + 观测计数，避免跨用例串味。"""
    R._TG_PEER_IDENTITY_CACHE.clear()
    R._TG_CLIENT_BAD_UNTIL.clear()
    get_peer_identity_stats().reset()
    yield
    R._TG_PEER_IDENTITY_CACHE.clear()
    R._TG_CLIENT_BAD_UNTIL.clear()
    get_peer_identity_stats().reset()


@pytest.fixture
def bg_loop():
    """后台线程跑一个真实事件循环，模拟 pyrogram client 自身 loop（与 web loop 分离）。"""
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=2)
    loop.close()


class _FakeChat:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _FakePyro:
    """鸭子类型的 pyrogram client：暴露 .loop + async get_chat。"""

    def __init__(self, loop, chat=None, raise_exc=False):
        self.loop = loop
        self._chat = chat
        self._raise = raise_exc
        self.calls = 0

    async def get_chat(self, peer):
        self.calls += 1
        if self._raise:
            raise RuntimeError("boom")
        return self._chat


class _FakeTG:
    """A 线 TelegramClient 包装器形态：.client 才是 pyro（主 client / companion worker.client）。"""

    def __init__(self, pyro):
        self.client = pyro


class _FakeApp:
    def __init__(self, telegram_client=None):
        self.state = types.SimpleNamespace(telegram_client=telegram_client)


# ─────────────────────── Telegram 解析核心 ───────────────────────

def test_resolve_happy_path_persists_and_returns(tmp_path, bg_loop):
    store = InboxStore(tmp_path / "inbox.db")
    # 存量：显名是裸数字 id
    store.upsert_conversation(InboxConversation(
        conversation_id="telegram:acct1:777", platform="telegram",
        account_id="acct1", chat_key="777", display_name="777", last_ts=10))
    chat = _FakeChat(first_name="Ivan", last_name="Petrov",
                     username="ivan_p", phone_number="+79001234567")
    tg = _FakeTG(_FakePyro(bg_loop, chat=chat))

    res = R.resolve_tg_peer_identity(store, tg, "acct1", "777")
    assert res["ok"] is True
    assert res["name"] == "Ivan Petrov"
    assert res["username"] == "ivan_p"
    assert res["phone"] == "+79001234567"
    # 落库后从会话读回：裸数字被补成真名 + username/phone 落地
    row = [c for c in store.list_conversations(platform="telegram")
           if c["conversation_id"] == "telegram:acct1:777"][0]
    assert row["display_name"] == "Ivan Petrov"
    assert row["username"] == "ivan_p"
    assert row["phone"] == "+79001234567"
    store.close()


def test_resolve_username_only_fallback(tmp_path, bg_loop):
    store = InboxStore(tmp_path / "inbox.db")
    chat = _FakeChat(username="justuser")   # 无 first/last → name 回落 @username
    tg = _FakeTG(_FakePyro(bg_loop, chat=chat))
    res = R.resolve_tg_peer_identity(store, tg, "acct1", "888")
    assert res["ok"] is True
    assert res["username"] == "justuser"
    assert res["name"] == "@justuser"       # tg_peer_identity：无名回落 @username
    store.close()


def test_resolve_cache_hit_avoids_second_get_chat(tmp_path, bg_loop):
    store = InboxStore(tmp_path / "inbox.db")
    pyro = _FakePyro(bg_loop, chat=_FakeChat(first_name="Ann"))
    tg = _FakeTG(pyro)
    r1 = R.resolve_tg_peer_identity(store, tg, "acct1", "111")
    r2 = R.resolve_tg_peer_identity(store, tg, "acct1", "111")
    assert r1["ok"] is True and r2["ok"] is True
    assert r2["name"] == "Ann"
    assert pyro.calls == 1                   # 第二次命中缓存，未再打 get_chat
    store.close()


def test_resolve_failed_is_negative_cached(tmp_path, bg_loop):
    store = InboxStore(tmp_path / "inbox.db")
    pyro = _FakePyro(bg_loop, raise_exc=True)
    tg = _FakeTG(pyro)
    r1 = R.resolve_tg_peer_identity(store, tg, "acct1", "222")
    r2 = R.resolve_tg_peer_identity(store, tg, "acct1", "222")
    assert r1["ok"] is False and r1["reason"] == "resolve_failed"
    assert r2["ok"] is False                 # 负缓存
    assert pyro.calls == 1                    # 未重复打 API
    store.close()


def test_resolve_empty_identity_returns_false_and_keeps_existing(tmp_path, bg_loop):
    store = InboxStore(tmp_path / "inbox.db")
    store.upsert_conversation(InboxConversation(
        conversation_id="telegram:acct1:333", platform="telegram",
        account_id="acct1", chat_key="333", display_name="老板", last_ts=10))
    # get_chat 返回没有任何可用身份的 chat → ok:false，且不冲掉已有真名
    tg = _FakeTG(_FakePyro(bg_loop, chat=_FakeChat()))
    res = R.resolve_tg_peer_identity(store, tg, "acct1", "333")
    assert res["ok"] is False
    row = [c for c in store.list_conversations(platform="telegram")
           if c["conversation_id"] == "telegram:acct1:333"][0]
    assert row["display_name"] == "老板"      # 未被清空
    store.close()


def test_resolve_client_unavailable_not_cached(tmp_path):
    """client 未连接（loop 未跑）→ ok:false/client_unavailable，且不写缓存（下次上线重试）。"""
    store = InboxStore(tmp_path / "inbox.db")
    dead = asyncio.new_event_loop()           # 从不 run_forever → is_running()=False
    tg = _FakeTG(_FakePyro(dead))
    res = R.resolve_tg_peer_identity(store, tg, "acct1", "444")
    assert res["ok"] is False and res["reason"] == "client_unavailable"
    assert ("acct1", "444") not in R._TG_PEER_IDENTITY_CACHE
    dead.close()
    store.close()


def test_resolve_bad_input_is_false(tmp_path, bg_loop):
    store = InboxStore(tmp_path / "inbox.db")
    tg = _FakeTG(_FakePyro(bg_loop, chat=_FakeChat(first_name="X")))
    assert R.resolve_tg_peer_identity(store, tg, "acct1", "")["ok"] is False   # 空 chat_key
    assert R.resolve_tg_peer_identity(None, tg, "acct1", "1")["ok"] is False    # 无 store
    store.close()


def test_resolve_no_telegram_client(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    assert R.resolve_tg_peer_identity(store, None, "acct1", "1")["ok"] is False
    store.close()


def test_peer_cache_bounded_eviction(monkeypatch):
    """缓存达上限时按最旧写入逐出，最新写入保留、总量受控。"""
    monkeypatch.setattr(R, "_TG_PEER_IDENTITY_CACHE_MAX", 10)
    R._TG_PEER_IDENTITY_CACHE.clear()
    for i in range(15):
        R._cache_tg_peer_identity(("acct", str(i)), {"name": f"n{i}"}, ts=float(i))
    assert len(R._TG_PEER_IDENTITY_CACHE) <= 10
    assert ("acct", "14") in R._TG_PEER_IDENTITY_CACHE     # 最新保留
    assert ("acct", "0") not in R._TG_PEER_IDENTITY_CACHE   # 最旧被逐出


# ─────────────────────── 断网冷却（get_chat 超时 → 账号级快速失败） ───────────────────────

class _HangPyro:
    """get_chat 永挂（模拟 pyrogram 断网重连）：等一个不会来的事件。"""

    def __init__(self, loop):
        self.loop = loop
        self.calls = 0

    async def get_chat(self, peer):
        self.calls += 1
        await asyncio.sleep(3600)


def test_resolve_timeout_opens_cooldown_and_fast_fails(tmp_path, bg_loop, monkeypatch):
    """首个 get_chat 超时 → 开账号冷却；冷却窗内同账号 resolve 秒失败（零 RPC），
    且不写 per-peer 负缓存（恢复后同 peer 仍可重试）。"""
    monkeypatch.setattr(R, "_TG_FETCH_TIMEOUT_SEC", 0.2)   # 免等 8s
    store = InboxStore(tmp_path / "inbox.db")
    pyro = _HangPyro(bg_loop)
    tg = _FakeTG(pyro)

    r1 = R.resolve_tg_peer_identity(store, tg, "acct1", "111")
    assert r1["ok"] is False and r1["reason"] == "resolve_timeout"
    assert pyro.calls == 1
    assert R._tg_client_cooling("acct1") is True            # 冷却已开
    assert ("acct1", "111") not in R._TG_PEER_IDENTITY_CACHE  # 未写 per-peer 负缓存

    r2 = R.resolve_tg_peer_identity(store, tg, "acct1", "222")   # 另一 peer
    assert r2["ok"] is False and r2["reason"] == "client_cooling"
    assert pyro.calls == 1                                   # 冷却窗内零新 RPC
    store.close()


def test_resolve_success_clears_cooldown(tmp_path, bg_loop):
    """冷却窗过期后 RPC 成功 → 冷却解除，后续正常解析。"""
    store = InboxStore(tmp_path / "inbox.db")
    tg = _FakeTG(_FakePyro(bg_loop, chat=_FakeChat(first_name="Ann")))
    # 人工造一个「已过期」的冷却（monotonic 过去时刻）
    import time as _t
    R._TG_CLIENT_BAD_UNTIL["acct1"] = _t.monotonic() - 1
    res = R.resolve_tg_peer_identity(store, tg, "acct1", "333")
    assert res["ok"] is True and res["name"] == "Ann"
    assert "acct1" not in R._TG_CLIENT_BAD_UNTIL             # 成功后冷却字典已清
    store.close()


def test_cooldown_is_per_account(tmp_path, bg_loop):
    """冷却按账号隔离：acct1 冷却中，acct2 不受影响。"""
    store = InboxStore(tmp_path / "inbox.db")
    R._mark_tg_client_bad("acct1")
    tg = _FakeTG(_FakePyro(bg_loop, chat=_FakeChat(first_name="Bob")))
    r_cool = R.resolve_tg_peer_identity(store, tg, "acct1", "1")
    assert r_cool["reason"] == "client_cooling"
    r_ok = R.resolve_tg_peer_identity(store, tg, "acct2", "1")
    assert r_ok["ok"] is True and r_ok["name"] == "Bob"
    store.close()


# ─────────────────────── LINE 私聊发送者显示名 ───────────────────────

class _FakeLineClient:
    """鸭子类型的 okline OkLine：get_contacts 返回 getContactsV2 形态。"""

    def __init__(self, mapping):
        self._mapping = mapping   # {mid: {"displayName":..,"displayNameOverridden":..}}
        self.calls = 0

    def get_contacts(self, mids):
        self.calls += 1
        contacts = {}
        for m in mids:
            if m in self._mapping:
                contacts[m] = {"contact": self._mapping[m]}
        return {"contacts": contacts}


def _line_worker():
    from src.integrations.account_orchestrator import LineProtocolWorker
    return LineProtocolWorker({"account_id": "lineA"}, {})


def test_line_resolve_prefers_overridden_name():
    okline = pytest.importorskip("okline")   # 无 okline → 跳过（Contact.from_dict 依赖）
    assert okline
    w = _line_worker()
    w.client = _FakeLineClient({"u_mid": {"displayName": "Ivan",
                                          "displayNameOverridden": "老王"}})
    assert w._resolve_peer_name("u_mid") == "老王"   # 备注名优先


def test_line_resolve_falls_back_to_display_name():
    pytest.importorskip("okline")
    w = _line_worker()
    w.client = _FakeLineClient({"u_mid": {"displayName": "Ivan"}})
    assert w._resolve_peer_name("u_mid") == "Ivan"


def test_line_resolve_missing_contact_is_empty_and_cached():
    pytest.importorskip("okline")
    w = _line_worker()
    client = _FakeLineClient({})              # 该 mid 不在返回里
    w.client = client
    assert w._resolve_peer_name("stranger") == ""
    assert w._resolve_peer_name("stranger") == ""
    assert client.calls == 1                  # 空结果也缓存，不逐条重打


def test_line_resolve_caches_hit():
    pytest.importorskip("okline")
    w = _line_worker()
    client = _FakeLineClient({"u_mid": {"displayName": "Ann"}})
    w.client = client
    assert w._resolve_peer_name("u_mid") == "Ann"
    assert w._resolve_peer_name("u_mid") == "Ann"
    assert client.calls == 1                  # 第二次命中缓存


def test_line_resolve_swallows_errors():
    pytest.importorskip("okline")
    w = _line_worker()

    class _Boom:
        def get_contacts(self, mids):
            raise RuntimeError("network")

    w.client = _Boom()
    assert w._resolve_peer_name("u_mid") == ""   # 异常吞掉回空，不抛
    assert w._resolve_peer_name("") == ""         # 空 mid 直接空


def test_line_resolve_identity_extracts_avatar():
    # 头像随同一次 get_contacts 免费取得：picturePath → 稳定 obs 直链，(name, avatar) 一并返回+缓存
    pytest.importorskip("okline")
    w = _line_worker()
    client = _FakeLineClient({"u_mid": {"displayName": "Ivan", "picturePath": "/p/av1"}})
    w.client = client
    name, avatar = w._resolve_peer_identity("u_mid")
    assert name == "Ivan"
    assert avatar.startswith("http") and avatar.endswith("/p/av1")
    # 二次命中缓存（名字+头像同缓存，不重打 API）
    assert w._resolve_peer_identity("u_mid") == (name, avatar)
    assert client.calls == 1


def test_line_resolve_identity_no_picture_is_empty_avatar():
    pytest.importorskip("okline")
    w = _line_worker()
    w.client = _FakeLineClient({"u_mid": {"displayName": "Ann"}})   # 无 picturePath
    name, avatar = w._resolve_peer_identity("u_mid")
    assert name == "Ann" and avatar == ""


def test_line_resolve_name_wrapper_backcompat():
    # 向后兼容薄封装：仍只回显示名（内部走 _resolve_peer_identity）
    pytest.importorskip("okline")
    w = _line_worker()
    w.client = _FakeLineClient({"u_mid": {"displayName": "Ann", "picturePath": "/p/x"}})
    assert w._resolve_peer_name("u_mid") == "Ann"
    assert w._resolve_peer_name("") == ""


# ─────────────────── 共享落库+缓存 helper（item 1 DRY） ───────────────────

def test_persist_and_cache_helper_backfills_and_warms_cache(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    store.upsert_conversation(InboxConversation(
        conversation_id="telegram:acct1:999", platform="telegram",
        account_id="acct1", chat_key="999", display_name="999", last_ts=10))
    res = R._persist_and_cache_tg_identity(
        store, "acct1", "999", {"name": "Real Name", "username": "rn", "phone": "+100"})
    assert res == {"name": "Real Name", "username": "rn", "phone": "+100"}
    row = [c for c in store.list_conversations(platform="telegram")
           if c["conversation_id"] == "telegram:acct1:999"][0]
    assert row["display_name"] == "Real Name"     # 裸数字被补真名
    assert row["username"] == "rn"
    assert ("acct1", "999") in R._TG_PEER_IDENTITY_CACHE   # 暖了 resolve 缓存（正）
    store.close()


def test_persist_and_cache_helper_empty_writes_negative_cache(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    res = R._persist_and_cache_tg_identity(
        store, "acct1", "1", {"name": "", "username": "", "phone": ""})
    assert res == {"name": "", "username": "", "phone": ""}
    assert R._TG_PEER_IDENTITY_CACHE[("acct1", "1")][1] is None   # 负缓存
    store.close()


def test_persist_and_cache_helper_no_store_still_caches(tmp_path):
    """store=None（会话尚未落库）→ 不抛，仍写缓存供后续复用。"""
    res = R._persist_and_cache_tg_identity(None, "acct1", "5", {"name": "X", "username": "", "phone": ""})
    assert res["name"] == "X"
    assert ("acct1", "5") in R._TG_PEER_IDENTITY_CACHE


# ─────────────────── 观测计数（item 4：peer_identity_stats） ───────────────────

def test_stats_record_dump_and_ignore_unknown():
    s = PeerIdentityStats()
    s.record("tg_open", "resolved")
    s.record("tg_open", "resolved")
    s.record("tg_avatar", "resolved")
    s.record("tg_open", "cache_hit")
    s.record("line", "miss")
    s.record("bogus", "resolved")     # 未知 source → 忽略
    s.record("line", "bogus")          # 未知 outcome → 忽略
    d = s.dump()
    assert d["resolved"] == 3
    assert d["cache_hits"] == 1
    assert d["misses"] == 1
    assert d["total"] == 5             # 两条未知未计入
    assert d["by_source"]["tg_open"]["resolved"] == 2
    assert d["by_source"]["tg_avatar"]["resolved"] == 1
    assert d["by_source"]["line"]["miss"] == 1


def test_stats_prom_format():
    s = PeerIdentityStats()
    s.record("tg_open", "resolved")
    s.record("line", "cache_hit")
    out = s.dump_prom()
    assert "# TYPE peer_identity_resolve_total counter" in out
    assert 'peer_identity_resolve_total{source="tg_open",outcome="resolved"} 1' in out
    assert 'peer_identity_resolve_total{source="line",outcome="cache_hit"} 1' in out


def test_stats_route_aggregate_and_by_account():
    s = PeerIdentityStats()
    s.record_route("worker", "acctA")
    s.record_route("worker", "acctA")
    s.record_route("fallback", "acctB")
    s.record_route("none", "acctB")
    s.record_route("worker")            # 无 account_id → 只记聚合
    s.record_route("bogus", "acctA")    # 未知 outcome → 忽略
    rt = s.dump()["routing"]
    assert rt["total"] == 5             # 4 有账号 + 1 无账号；bogus 未计
    assert rt["worker"] == 3 and rt["fallback"] == 1 and rt["none"] == 1
    assert rt["by_account"]["acctA"] == {"worker": 2, "fallback": 0, "none": 0}
    assert rt["by_account"]["acctB"] == {"worker": 0, "fallback": 1, "none": 1}
    assert "" not in rt["by_account"]   # 空账号不建槽


def test_stats_route_by_account_bounded(monkeypatch):
    import src.web.peer_identity_stats as PS
    monkeypatch.setattr(PS, "_ROUTE_ACCT_MAX", 3)
    s = PeerIdentityStats()
    for i in range(10):
        s.record_route("fallback", f"acct{i}")
    rt = s.dump()["routing"]
    assert rt["fallback"] == 10                 # 聚合恒记全
    assert len(rt["by_account"]) == 3           # distinct 账号封顶


def test_stats_route_prom_and_reset():
    s = PeerIdentityStats()
    s.record_route("worker", "acctA")
    s.record_route("fallback", "acctB")
    out = s.dump_prom()
    assert "# TYPE peer_identity_client_route_total counter" in out
    assert 'peer_identity_client_route_total{outcome="worker"} 1' in out
    assert 'peer_identity_client_route_total{outcome="fallback"} 1' in out
    assert 'peer_identity_client_route_total{outcome="none"} 0' in out
    s.reset()
    rt = s.dump()["routing"]
    assert rt["total"] == 0 and rt["by_account"] == {}


def test_resolve_core_records_resolved_then_cache_hit(tmp_path, bg_loop):
    store = InboxStore(tmp_path / "inbox.db")
    tg = _FakeTG(_FakePyro(bg_loop, chat=_FakeChat(first_name="Ivan")))
    R.resolve_tg_peer_identity(store, tg, "acct1", "777")    # resolved
    R.resolve_tg_peer_identity(store, tg, "acct1", "777")    # cache_hit
    by = get_peer_identity_stats().dump()["by_source"]["tg_open"]
    assert by["resolved"] == 1
    assert by["cache_hit"] == 1
    store.close()


def test_resolve_core_records_unavailable(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    dead = asyncio.new_event_loop()          # 未 run → is_running()=False
    tg = _FakeTG(_FakePyro(dead))
    R.resolve_tg_peer_identity(store, tg, "acct1", "1")
    assert get_peer_identity_stats().dump()["unavailable"] == 1
    dead.close()
    store.close()


def test_resolve_core_records_miss_on_failure(tmp_path, bg_loop):
    store = InboxStore(tmp_path / "inbox.db")
    tg = _FakeTG(_FakePyro(bg_loop, raise_exc=True))
    R.resolve_tg_peer_identity(store, tg, "acct1", "2")
    assert get_peer_identity_stats().dump()["misses"] == 1
    store.close()


# ─────────────────── 多账号 client 路由（_extract_pyro / worker_for / 取数入口） ───────────────────

def test_extract_pyro_direct_wrapper_and_garbage(bg_loop):
    pyro = _FakePyro(bg_loop, chat=_FakeChat(first_name="A"))
    assert R._extract_pyro(pyro) is pyro              # protocol worker.client：已是 pyro
    assert R._extract_pyro(_FakeTG(pyro)) is pyro      # A 线包装器：取 .client
    assert R._extract_pyro(None) is None
    assert R._extract_pyro(object()) is None           # 既非 pyro 也无可用 .client


def test_get_tg_pyro_falls_back_to_main_client(bg_loop, monkeypatch):
    """编排器无该账号 worker → 回落进程主 client（单号/主账号，旧行为）。"""
    import src.integrations.account_orchestrator as AO
    pyro = _FakePyro(bg_loop, chat=_FakeChat(first_name="Main"))
    app = _FakeApp(telegram_client=_FakeTG(pyro))

    class _EmptyOrch:
        def worker_for(self, platform, account_id):
            return None

    monkeypatch.setattr(AO, "get_orchestrator_if_running", lambda: _EmptyOrch())
    assert R._get_tg_pyro_for_account(app, "acct1") is pyro
    rt = get_peer_identity_stats().dump()["routing"]
    assert rt["fallback"] == 1 and rt["worker"] == 0
    assert rt["by_account"]["acct1"]["fallback"] == 1


def test_get_tg_pyro_prefers_protocol_worker(bg_loop, monkeypatch):
    """protocol B 线 worker：.client 直接是 pyro，且优先于主 client。"""
    import src.integrations.account_orchestrator as AO
    worker_pyro = _FakePyro(bg_loop, chat=_FakeChat(first_name="Worker"))
    main_pyro = _FakePyro(bg_loop, chat=_FakeChat(first_name="Main"))
    app = _FakeApp(telegram_client=_FakeTG(main_pyro))

    class _Worker:
        client = worker_pyro

    class _Orch:
        def worker_for(self, platform, account_id):
            return _Worker()

    monkeypatch.setattr(AO, "get_orchestrator_if_running", lambda: _Orch())
    assert R._get_tg_pyro_for_account(app, "acctX") is worker_pyro
    rt = get_peer_identity_stats().dump()["routing"]
    assert rt["worker"] == 1 and rt["fallback"] == 0
    assert rt["by_account"]["acctX"]["worker"] == 1


def test_get_tg_pyro_companion_worker_wrapper(bg_loop, monkeypatch):
    """companion A 线 worker：.client 是包装器，其 .client 才是 pyro。"""
    import src.integrations.account_orchestrator as AO
    inner = _FakePyro(bg_loop, chat=_FakeChat(first_name="Comp"))
    app = _FakeApp(telegram_client=None)

    class _CompWorker:
        client = _FakeTG(inner)

    class _Orch:
        def worker_for(self, platform, account_id):
            return _CompWorker()

    monkeypatch.setattr(AO, "get_orchestrator_if_running", lambda: _Orch())
    assert R._get_tg_pyro_for_account(app, "acctX") is inner
    assert get_peer_identity_stats().dump()["routing"]["worker"] == 1


def test_get_tg_pyro_orchestrator_error_falls_back(bg_loop, monkeypatch):
    """取 worker 抛异常 → 吞掉并回落主 client（绝不因编排器问题拖垮头像/补名）。"""
    import src.integrations.account_orchestrator as AO
    pyro = _FakePyro(bg_loop, chat=_FakeChat(first_name="Main"))
    app = _FakeApp(telegram_client=_FakeTG(pyro))

    def _boom(*a, **k):
        raise RuntimeError("orch down")

    monkeypatch.setattr(AO, "get_orchestrator_if_running", _boom)
    assert R._get_tg_pyro_for_account(app, "acct1") is pyro
    assert get_peer_identity_stats().dump()["routing"]["fallback"] == 1


def test_get_tg_pyro_none_when_no_client_records_none(monkeypatch):
    """既无受管 worker 又无主 client → 返回 None 且记 routing=none（TG 整体离线信号）。"""
    import src.integrations.account_orchestrator as AO
    monkeypatch.setattr(AO, "get_orchestrator_if_running", lambda: None)
    app = _FakeApp(telegram_client=None)
    assert R._get_tg_pyro_for_account(app, "acctZ") is None
    rt = get_peer_identity_stats().dump()["routing"]
    assert rt["none"] == 1 and rt["worker"] == 0 and rt["fallback"] == 0
    assert rt["by_account"]["acctZ"]["none"] == 1


def test_orchestrator_worker_for_running_only():
    from src.integrations.account_orchestrator import (
        AccountOrchestrator, account_key, _Managed,
    )
    orch = AccountOrchestrator(registry=object(), config={})   # 非 None registry → 跳过单例
    key = account_key("telegram", "acctX")

    class _W:
        client = "pyro-ish"

    w = _W()
    orch._managed[key] = _Managed(key=key, platform="telegram", account_id="acctX",
                                  mode="protocol", worker=w, state="running")
    assert orch.worker_for("telegram", "acctX") is w
    orch._managed[key].state = "error"                          # 非 running → None
    assert orch.worker_for("telegram", "acctX") is None
    assert orch.worker_for("telegram", "nope") is None          # 未受管 → None
