"""P3：运营级「默认译文显示语言」——store KV + 端点解析优先级（账号>平台>全局）。

覆盖：
- InboxStore.app_settings 读写 + 空串清除（migration 建表生效）。
- GET/POST /api/unified-inbox/default-lang：按 scope 持久化、按优先级解析、清除回落、参数校验。
"""

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.inbox.store import InboxStore
from src.web.routes.unified_inbox_routes import register_unified_inbox_routes


class _Templates:
    def TemplateResponse(self, request, name, context):
        raise AssertionError("page rendering is not used in API tests")


def _client(tmp_path):
    app = FastAPI()

    def page_auth(request: Request):
        return True

    def api_auth(request: Request):
        return True

    register_unified_inbox_routes(
        app, page_auth=page_auth, api_auth=api_auth, templates=_Templates(),
    )
    app.state.inbox_store = InboxStore(tmp_path / "inbox.db")
    return TestClient(app)


# ── store 层 ────────────────────────────────────────────────────────────────

def test_app_setting_roundtrip_and_clear(tmp_path):
    store = InboxStore(tmp_path / "s.db")
    assert store.get_app_setting("k.missing", "def") == "def"
    assert store.set_app_setting("k.lang", "th") is True
    assert store.get_app_setting("k.lang") == "th"
    # 空串 → 删除（视为未配置），回落 default
    assert store.set_app_setting("k.lang", "") is True
    assert store.get_app_setting("k.lang", "fallback") == "fallback"
    # 空键 → 拒绝
    assert store.set_app_setting("", "x") is False


def test_app_setting_list_prefix_and_audit(tmp_path):
    store = InboxStore(tmp_path / "s.db")
    store.set_app_setting("inbox.default_lang", "en", updated_by="alice")
    store.set_app_setting("inbox.default_lang.platform.telegram", "ja", updated_by="bob")
    store.set_app_setting("other.key", "x")
    rows = store.list_app_settings("inbox.default_lang")
    keys = {r["key"]: r for r in rows}
    assert set(keys) == {"inbox.default_lang", "inbox.default_lang.platform.telegram"}
    assert keys["inbox.default_lang"]["value"] == "en"
    assert keys["inbox.default_lang"]["updated_by"] == "alice"
    assert keys["inbox.default_lang.platform.telegram"]["updated_by"] == "bob"


# ── 端点层 ──────────────────────────────────────────────────────────────────

def test_default_lang_resolution_precedence(tmp_path):
    c = _client(tmp_path)
    # 三个维度各设一值
    assert c.post("/api/unified-inbox/default-lang",
                  json={"scope": "global", "lang": "en"}).json()["ok"]
    assert c.post("/api/unified-inbox/default-lang",
                  json={"scope": "platform", "platform": "telegram", "lang": "ja"}).json()["ok"]
    assert c.post("/api/unified-inbox/default-lang",
                  json={"scope": "account", "platform": "telegram",
                        "account_id": "acc1", "lang": "th"}).json()["ok"]

    # 账号命中 → th（最高优先级），scopes 全维度回显
    d = c.get("/api/unified-inbox/default-lang?platform=telegram&account_id=acc1").json()
    assert d["ok"] and d["resolved"] == "th"
    assert d["scopes"] == {"global": "en", "platform": "ja", "account": "th"}

    # 同平台其它账号 → 回落平台 ja
    d = c.get("/api/unified-inbox/default-lang?platform=telegram&account_id=other").json()
    assert d["resolved"] == "ja"

    # 其它平台 → 回落全局 en
    d = c.get("/api/unified-inbox/default-lang?platform=line&account_id=x").json()
    assert d["resolved"] == "en"


def test_default_lang_clear_falls_back(tmp_path):
    c = _client(tmp_path)
    c.post("/api/unified-inbox/default-lang", json={"scope": "platform", "platform": "telegram", "lang": "ja"})
    c.post("/api/unified-inbox/default-lang",
           json={"scope": "account", "platform": "telegram", "account_id": "acc1", "lang": "th"})
    # 清除账号默认（lang="") → 回落平台 ja
    c.post("/api/unified-inbox/default-lang",
           json={"scope": "account", "platform": "telegram", "account_id": "acc1", "lang": ""})
    d = c.get("/api/unified-inbox/default-lang?platform=telegram&account_id=acc1").json()
    assert d["resolved"] == "ja"


def test_default_lang_normalizes_noncanonical(tmp_path):
    c = _client(tmp_path)
    # zh-cn → zh 归一后存储
    c.post("/api/unified-inbox/default-lang", json={"scope": "global", "lang": "zh-cn"})
    d = c.get("/api/unified-inbox/default-lang?platform=telegram&account_id=acc1").json()
    assert d["resolved"] == "zh"
    assert d["scopes"]["global"] == "zh"


def test_default_lang_unconfigured_returns_empty(tmp_path):
    c = _client(tmp_path)
    d = c.get("/api/unified-inbox/default-lang?platform=telegram&account_id=acc1").json()
    assert d["ok"] is True
    assert d["resolved"] == ""
    assert d["scopes"] == {"global": "", "platform": "", "account": ""}


def test_default_lang_list_all_parses_scopes_and_audit(tmp_path):
    c = _client(tmp_path)
    c.post("/api/unified-inbox/default-lang",
           json={"scope": "global", "lang": "en", "updated_by": "alice"})
    c.post("/api/unified-inbox/default-lang",
           json={"scope": "platform", "platform": "telegram", "lang": "ja", "updated_by": "bob"})
    c.post("/api/unified-inbox/default-lang",
           json={"scope": "account", "platform": "telegram", "account_id": "acc1",
                 "lang": "th", "updated_by": "carol"})
    d = c.get("/api/unified-inbox/default-lang/all").json()
    assert d["ok"] is True
    by_scope = {(it["scope"], it["platform"], it["account_id"]): it for it in d["items"]}
    assert by_scope[("global", "", "")]["lang"] == "en"
    assert by_scope[("global", "", "")]["updated_by"] == "alice"
    assert by_scope[("platform", "telegram", "")]["lang"] == "ja"
    assert by_scope[("account", "telegram", "acc1")]["lang"] == "th"
    assert by_scope[("account", "telegram", "acc1")]["updated_by"] == "carol"
    # 清除后不再出现在列表
    c.post("/api/unified-inbox/default-lang",
           json={"scope": "platform", "platform": "telegram", "lang": ""})
    d2 = c.get("/api/unified-inbox/default-lang/all").json()
    assert not any(it["scope"] == "platform" for it in d2["items"])


def test_default_lang_scope_validation(tmp_path):
    c = _client(tmp_path)
    # account/platform 缺 platform → 报错且不落库
    assert c.post("/api/unified-inbox/default-lang",
                  json={"scope": "account", "lang": "th"}).json()["error"] == "missing_platform"
    assert c.post("/api/unified-inbox/default-lang",
                  json={"scope": "platform", "lang": "th"}).json()["error"] == "missing_platform"
    # 非法 scope
    assert c.post("/api/unified-inbox/default-lang",
                  json={"scope": "bogus", "lang": "th"}).json()["error"] == "bad_scope"


# ── P4-C：默认回复语言（出站轴，桌面草稿默认）────────────────────────────────

def test_default_reply_lang_resolution_precedence(tmp_path):
    c = _client(tmp_path)
    assert c.post("/api/unified-inbox/default-reply-lang",
                  json={"scope": "global", "lang": "en"}).json()["ok"]
    assert c.post("/api/unified-inbox/default-reply-lang",
                  json={"scope": "platform", "platform": "telegram", "lang": "ja"}).json()["ok"]
    assert c.post("/api/unified-inbox/default-reply-lang",
                  json={"scope": "account", "platform": "telegram",
                        "account_id": "acc1", "lang": "th"}).json()["ok"]
    d = c.get("/api/unified-inbox/default-reply-lang?platform=telegram&account_id=acc1").json()
    assert d["ok"] and d["resolved"] == "th"
    assert d["scopes"] == {"global": "en", "platform": "ja", "account": "th"}
    # 同平台其它账号 → 平台 ja；其它平台 → 全局 en
    assert c.get("/api/unified-inbox/default-reply-lang?platform=telegram&account_id=other").json()["resolved"] == "ja"
    assert c.get("/api/unified-inbox/default-reply-lang?platform=line&account_id=x").json()["resolved"] == "en"


def test_default_reply_lang_list_all_and_audit(tmp_path):
    c = _client(tmp_path)
    c.post("/api/unified-inbox/default-reply-lang",
           json={"scope": "platform", "platform": "telegram", "lang": "th", "updated_by": "dave"})
    d = c.get("/api/unified-inbox/default-reply-lang/all").json()
    assert d["ok"] is True
    by_scope = {(it["scope"], it["platform"], it["account_id"]): it for it in d["items"]}
    assert by_scope[("platform", "telegram", "")]["lang"] == "th"
    assert by_scope[("platform", "telegram", "")]["updated_by"] == "dave"


def test_reply_lang_and_display_lang_are_isolated(tmp_path):
    """两轴键命名空间不串：设回复语言不影响显示语言，反之亦然（防 LIKE 前缀误匹配）。"""
    c = _client(tmp_path)
    c.post("/api/unified-inbox/default-lang", json={"scope": "global", "lang": "zh"})
    c.post("/api/unified-inbox/default-reply-lang", json={"scope": "global", "lang": "th"})
    # 各自解析互不影响
    assert c.get("/api/unified-inbox/default-lang?platform=telegram&account_id=a").json()["resolved"] == "zh"
    assert c.get("/api/unified-inbox/default-reply-lang?platform=telegram&account_id=a").json()["resolved"] == "th"
    # /all 各自只含自己那一轴
    disp = c.get("/api/unified-inbox/default-lang/all").json()["items"]
    repl = c.get("/api/unified-inbox/default-reply-lang/all").json()["items"]
    assert all(it["lang"] == "zh" for it in disp) and len(disp) == 1
    assert all(it["lang"] == "th" for it in repl) and len(repl) == 1
