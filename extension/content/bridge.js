"use strict";

/* 扩展 content script Host 适配层（薄）——等价于桌面 desktop/inject/tg-inject.js。
 * 复用单一源 shared/inject 的 profiles / media-format / core（前序 content_scripts 已把它们
 * 挂到本隔离世界的 globalThis）。这里只把 core 需要的 host 能力用 chrome.runtime 实现：
 * 业务请求 → 转发给 background service worker（它持 host_permissions 跨域打后端）。
 */

(function () {
  const profiles = globalThis.AInjectProfiles;
  const mediaFormat = globalThis.AInjectMediaFormat || null;
  const core = globalThis.AInjectCore;
  if (!profiles || !core || typeof core.createInject !== "function") {
    try { console.warn("[ai-inject] shared 模块缺失，注入未启动"); } catch (e) {}
    return;
  }

  function send(type, args) {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage({ type, args }, (resp) => {
          if (chrome.runtime.lastError) { resolve(null); return; }
          resolve(resp);
        });
      } catch (e) {
        resolve(null);
      }
    });
  }

  // 人设 / 回复语言 / 账号：来自扩展设置（chrome.storage.local），并监听变化实时下发给 core。
  let personaCb = null;
  let replyLangCb = null;
  let accountCb = null;

  function pushOne(key, cb, wrap) {
    if (!cb) return;
    try {
      chrome.storage.local.get(key, (i) => cb(wrap((i || {})[key])));
    } catch (e) { /* ignore */ }
  }

  const host = {
    translate: (a) => send("translate", a),
    translateMedia: (a) => send("translateMedia", a),
    smartReply: (a) => send("smartReply", a),
    ingest: (a) => send("ingest", a),
    getConfig: () => send("getConfig"),
    getSelectorProfiles: () => send("getSelectorProfiles"),
    diag: (m) => { try { console.debug("[ai-inject]", m); } catch (e) {} },
    injectHealth: (p) => { send("injectHealth", p); },
    // 扩展无桌面宿主右栏：把最近状态/会话存入 storage，供 popup/options 读取。
    reportInjectStatus: (p) => { try { chrome.storage.local.set({ last_inject_status: p }); } catch (e) {} },
    reportActiveChat: (p) => { try { chrome.storage.local.set({ last_active_chat: p }); } catch (e) {} },
    onSetPersona: (cb) => { personaCb = cb; pushOne("persona_id", cb, (v) => ({ persona_id: v || "" })); },
    onSetReplyLang: (cb) => { replyLangCb = cb; pushOne("reply_lang", cb, (v) => ({ target_lang: v || "" })); },
    onSetAccount: (cb) => { accountCb = cb; pushOne("account_id", cb, (v) => ({ account_id: v || "" })); },
    // 扩展场景暂无「宿主下发填入」入口（填入在本页内由智能回复浮钮直接完成）。
    onFillComposer: () => {},
  };

  try {
    chrome.storage.onChanged.addListener((changes, area) => {
      if (area !== "local") return;
      if (changes.persona_id && personaCb) personaCb({ persona_id: changes.persona_id.newValue || "" });
      if (changes.reply_lang && replyLangCb) replyLangCb({ target_lang: changes.reply_lang.newValue || "" });
      if (changes.account_id && accountCb) accountCb({ account_id: changes.account_id.newValue || "" });
    });
  } catch (e) { /* ignore */ }

  core.createInject(host, { profiles, mediaFormat }).autostart();
})();
