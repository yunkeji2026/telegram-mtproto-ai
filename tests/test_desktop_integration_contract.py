"""桌面端 ↔ 后端集成契约回归（保护 desktop/ 统一收件箱内嵌方案）。

桌面壳（desktop/）把后台 /workspace 内嵌为统一收件箱，依赖以下后端契约。
这些契约一旦被后端改动破坏，桌面端会静默失效（前端纯 webview，CI 抓不到），
故在此用后端 TestClient 把契约锁死：

  1. GET  /login            未鉴权返回 200 —— 桌面主进程健康探针目标（可达性判断）
  2. POST /login auth_token  → 建立 session，随后 GET /workspace 返回 200
                              —— 桌面收件箱 webview 的 token 自动登录链路
  3. GET  /workspace        含 __desktopOpenConversation / convKey
                              —— 会话深链入口（内嵌平台 Tab → 收件箱定位同一会话）
  4. GET  /workspace?lang=  i18n 中间件按 ?lang= 渲染 <html lang=..>
                              —— unified_inbox.lang 语言对齐贯穿登录回跳
  5. GET  /api/unified-inbox/chats  返回 {ok, chats[], platform_status}
                              —— 收件箱会话列表数据形状（convKey 依赖 platform/chat_key）
"""


def test_health_probe_target_unauth_200(client):
    """桌面主进程 desktop:backend-health 探针打的是 GET /login，须免鉴权返回 200。"""
    r = client.get("/login", follow_redirects=False)
    assert r.status_code == 200


def test_token_autologin_then_workspace_ok(client):
    """复刻桌面默认配置（仅 token、无预建用户）的自动登录链路：
    POST /login(auth_token) 建会话 → GET /workspace 直接 200（而非 303 回 /login）。"""
    r = client.post(
        "/login", data={"auth_token": "test-token-123"}, follow_redirects=False
    )
    assert r.status_code in (302, 303), f"token 登录应重定向，实际 {r.status_code}"

    r2 = client.get("/workspace", follow_redirects=False)
    assert r2.status_code == 200, "token 自动登录后 /workspace 应可直接访问"
    assert "__desktopOpenConversation" in r2.text


def test_deeplink_entrypoint_present(auth_client):
    """会话深链：/workspace 必须暴露 __desktopOpenConversation 与 convKey。"""
    r = auth_client.get("/workspace", follow_redirects=False)
    assert r.status_code == 200
    body = r.text
    assert "__desktopOpenConversation" in body, "会话深链入口缺失（2A 回归）"
    assert "function convKey" in body, "convKey 约定缺失（深链定位依赖）"


def test_language_alignment_honors_lang_query(auth_client):
    """语言对齐：?lang= 应被 i18n 中间件采纳并体现在合法的 <html lang=..>。

    en → ``lang="en"``（③-S1 前曾错渲成非法的 ``en-CN``，现已修正为标准 BCP-47）；zh → ``zh-CN``。
    """
    en = auth_client.get("/workspace?lang=en", follow_redirects=False)
    assert en.status_code == 200
    assert 'lang="en"' in en.text, "?lang=en 未生效（语言对齐回归）"
    assert 'lang="en-CN"' not in en.text, "en 不应再渲成非法 en-CN（③-S1 lang 修复回归）"

    zh = auth_client.get("/workspace?lang=zh", follow_redirects=False)
    assert zh.status_code == 200
    assert 'lang="zh-CN"' in zh.text


def test_unified_inbox_chats_contract(auth_client):
    """收件箱会话列表数据形状：{ok, chats:list, platform_status:dict}。"""
    r = auth_client.get("/api/unified-inbox/chats?limit=10", follow_redirects=False)
    assert r.status_code == 200
    d = r.json()
    assert d.get("ok") is True
    assert isinstance(d.get("chats"), list)
    assert isinstance(d.get("platform_status"), dict)
    # chats 若非空，每条须带 convKey 依赖字段（platform / chat_key）
    for c in d["chats"]:
        assert "platform" in c and "chat_key" in c


def test_translate_image_contract(auth_client):
    """媒体翻译（图片）契约：POST translate-image 返回带 ok 的 JSON；
    未启用 vision 时须给出 reason/message（桌面 media-format 据此回显原因，而非静默失败）。"""
    r = auth_client.post(
        "/api/unified-inbox/translate-image",
        json={"image_b64": "Zm9v", "target_lang": "zh"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    d = r.json()
    assert "ok" in d
    if d.get("ok") is False:
        assert d.get("reason") or d.get("message"), "失败须带 reason/message 供前端回显"


def test_translate_voice_contract(auth_client):
    """媒体翻译（语音）契约：POST translate-voice 返回带 ok 的 JSON；
    未启用 ASR 时须给出 reason/message。"""
    r = auth_client.post(
        "/api/unified-inbox/translate-voice",
        json={"audio_b64": "Zm9v", "target_lang": "zh"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    d = r.json()
    assert "ok" in d
    if d.get("ok") is False:
        assert d.get("reason") or d.get("message"), "失败须带 reason/message 供前端回显"
