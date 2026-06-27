"use strict";

/* 扩展 service worker —— content script 的「后端代理」（等价于桌面 main.js 的主进程角色）。
 * content script 受同源/CORS 限制无法直接打后端；SW 持有 host_permissions，可跨域 fetch。
 * 这里的 backend* 函数忠实镜像 desktop/main.js 的请求/响应形状，让共享 core 在两端行为一致。
 */

const DEFAULTS = {
  base_url: "http://127.0.0.1:18799",
  token: "admin",
  target_lang: "zh",
  auto_translate: false,
  sync_enabled: false,
  debug: false,
};

async function settings() {
  const got = await chrome.storage.local.get(Object.keys(DEFAULTS));
  return Object.assign({}, DEFAULTS, got || {});
}

function authHeaders(token) {
  return { "Content-Type": "application/json", Authorization: `Bearer ${token}` };
}

async function backendTranslate(text, targetLang) {
  const s = await settings();
  const r = await fetch(`${s.base_url}/api/unified-inbox/translate`, {
    method: "POST",
    headers: authHeaders(s.token),
    body: JSON.stringify({ text, target_lang: targetLang || s.target_lang || "zh" }),
  });
  const d = await r.json();
  if (!d.ok) return "";
  const t = d.translation || {};
  return t.translated_text || t.text || t.translated || "";
}

async function backendTranslateMedia(kind, b64, targetLang) {
  const s = await settings();
  const isImg = kind === "image";
  const pathname = isImg ? "/api/unified-inbox/translate-image" : "/api/unified-inbox/translate-voice";
  const tgt = targetLang || s.target_lang || "zh";
  const body = isImg ? { image_b64: b64, target_lang: tgt } : { audio_b64: b64, target_lang: tgt };
  const r = await fetch(`${s.base_url}${pathname}`, {
    method: "POST",
    headers: authHeaders(s.token),
    body: JSON.stringify(body),
  });
  return await r.json();
}

async function backendSmartReply(payload) {
  const s = await settings();
  const p = payload || {};
  const r = await fetch(`${s.base_url}/api/desktop/smart-reply`, {
    method: "POST",
    headers: authHeaders(s.token),
    body: JSON.stringify({
      messages: Array.isArray(p.messages) ? p.messages : [],
      persona_id: p.persona_id || "",
      platform: p.platform || "",
      chat_key: p.chat_key || "",
      target_lang: p.target_lang || "",
    }),
  });
  return await r.json();
}

async function backendIngest(payload) {
  const s = await settings();
  const p = payload || {};
  const plat = String(p.platform || "");
  const account_id = p.account_id || `${plat}-ext`;
  const r = await fetch(`${s.base_url}/api/desktop/ingest`, {
    method: "POST",
    headers: authHeaders(s.token),
    body: JSON.stringify({ ...p, account_id }),
  });
  return await r.json();
}

async function backendGet(pathname, query) {
  const s = await settings();
  const qs = query
    ? "?" + new URLSearchParams(Object.entries(query).filter(([, v]) => v != null && v !== "")).toString()
    : "";
  const r = await fetch(`${s.base_url}${pathname}${qs}`, { headers: { Authorization: `Bearer ${s.token}` } });
  return await r.json();
}

async function backendPost(pathname, body) {
  const s = await settings();
  const r = await fetch(`${s.base_url}${pathname}`, {
    method: "POST",
    headers: authHeaders(s.token),
    body: JSON.stringify(body || {}),
  });
  return await r.json();
}

// core 的 getConfig：返回驱动注入行为的运行配置（不含 base_url/token，那些只 SW 自己用）。
async function coreConfig() {
  const s = await settings();
  return {
    translate: { target_lang: s.target_lang || "zh", auto: !!s.auto_translate },
    sync: { enabled: !!s.sync_enabled },
    debug: !!s.debug,
  };
}

// content script → SW 的统一消息路由。返回 Promise 形态，sendResponse 异步回传。
const HANDLERS = {
  translate: async (a) => {
    try { return { ok: true, text: await backendTranslate(String((a && a.text) || ""), a && a.target_lang) }; }
    catch (e) { return { ok: false, error: String(e) }; }
  },
  translateMedia: async (a) => {
    try { return await backendTranslateMedia((a && a.kind) === "image" ? "image" : "voice", String((a && a.b64) || ""), a && a.target_lang); }
    catch (e) { return { ok: false, error: String(e) }; }
  },
  smartReply: async (a) => {
    try { return await backendSmartReply(a || {}); }
    catch (e) { return { ok: false, error: String(e) }; }
  },
  ingest: async (a) => {
    try { return await backendIngest(a || {}); }
    catch (e) { return { ok: false, error: String(e) }; }
  },
  getConfig: async () => {
    try { return await coreConfig(); }
    catch (e) { return { translate: { target_lang: "zh", auto: false }, sync: { enabled: false } }; }
  },
  getSelectorProfiles: async () => {
    try { return await backendGet("/api/desktop/selector-profiles"); }
    catch (e) { return { ok: false, profiles: {}, error: String(e) }; }
  },
  injectHealth: async (a) => {
    try { return await backendPost("/api/desktop/inject-health", a || {}); }
    catch (e) { return { ok: false, error: String(e) }; }
  },
};

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  const handler = msg && HANDLERS[msg.type];
  if (!handler) return false;
  handler(msg.args).then(sendResponse).catch((e) => sendResponse({ ok: false, error: String(e) }));
  return true; // 异步 sendResponse
});
