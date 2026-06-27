"use strict";

const { app, BrowserWindow, ipcMain, session, clipboard, Menu } = require("electron");
const path = require("path");
const fs = require("fs");
const { chromeLikeUserAgent, isWhatsappUrl, needsChromeUa, urlNeedsChromeUa } = require("./webview-ua.js");
const { fingerprintArg, accountIdFromPartition } = require("./inject/fingerprint.js");

// D3：每账号确定性指纹缓存（account_id → fingerprint）。启动/运行时新增账号前拉取，
// 供 session UA / Accept-Language / webview additionalArguments 注入，使多号内嵌互不关联。
const FP_BY_ACCOUNT = {};
function fpEnabled() {
  const f = (config && config.fingerprint) || {};
  return f.enabled !== false; // 默认开启
}

const CONFIG_PATH = path.join(__dirname, "config.json");

function loadConfig() {
  try {
    return JSON.parse(fs.readFileSync(CONFIG_PATH, "utf-8"));
  } catch (e) {
    return {
      backend: { base_url: "http://127.0.0.1:18787", token: "admin" },
      translate: { target_lang: "zh", auto: false },
      platforms: [],
      accounts: [],
    };
  }
}

let config = loadConfig();

/** 调用后端翻译接口（主进程发起，规避 webview 的 CORS / 混合内容限制）。 */
async function backendTranslate(text, targetLang) {
  const { base_url, token } = config.backend || {};
  const r = await fetch(`${base_url}/api/unified-inbox/translate`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({ text, target_lang: targetLang || (config.translate || {}).target_lang || "zh" }),
  });
  const d = await r.json();
  if (!d.ok) return "";
  const t = d.translation || {};
  return t.translated_text || t.text || t.translated || "";
}

/** 调用后端媒体翻译：图片 OCR(/translate-image) 或 语音转写(/translate-voice) → 翻译。
 *  kind=image|voice；b64 为去掉 dataURL 前缀的纯 base64。主进程发起以规避 webview CORS/CSP。 */
async function backendTranslateMedia(kind, b64, targetLang) {
  const { base_url, token } = config.backend || {};
  const isImg = kind === "image";
  const pathname = isImg ? "/api/unified-inbox/translate-image" : "/api/unified-inbox/translate-voice";
  const tgt = targetLang || (config.translate || {}).target_lang || "zh";
  const body = isImg ? { image_b64: b64, target_lang: tgt } : { audio_b64: b64, target_lang: tgt };
  const r = await fetch(`${base_url}${pathname}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify(body),
  });
  return await r.json();
}

/** 调用后端智能回复（上下文版）。转发 persona_id/platform/chat_key，
 *  否则面板选的人设到不了后端、永远用默认人设。 */
async function backendSmartReply(payload) {
  const { base_url, token } = config.backend || {};
  const p = payload || {};
  const r = await fetch(`${base_url}/api/desktop/smart-reply`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
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

/** 把桌面端官方 web 看到的消息回流统一收件箱（P1 同步桥）。account_id 按平台从 config 补齐。 */
async function backendIngest(payload) {
  const { base_url, token } = config.backend || {};
  const plat = String(payload.platform || "");
  const pcfg = (config.platforms || []).find((p) => p.id === plat) || {};
  const account_id = payload.account_id || pcfg.account_id || `${plat}-desktop`;
  const r = await fetch(`${base_url}/api/desktop/ingest`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({ ...payload, account_id }),
  });
  return await r.json();
}

/** 通用后端 GET（主进程发起，规避 webview/renderer 的 CSP/CORS）。 */
async function backendGet(pathname, query) {
  const { base_url, token } = config.backend || {};
  const qs = query
    ? "?" + new URLSearchParams(Object.entries(query).filter(([, v]) => v != null && v !== "")).toString()
    : "";
  const r = await fetch(`${base_url}${pathname}${qs}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  return await r.json();
}

ipcMain.handle("desktop:apply-whatsapp-ua", async (_e, acc) => {
  await applyWhatsappSessionUa(acc || {});
  return { ok: true };
});

ipcMain.handle("desktop:config", () => ({
  ...config,
  whatsapp_user_agent: chromeLikeUserAgent(process.versions.chrome),
}));

/** 后端可达性探针（主进程发起，规避 webview/renderer 的 CSP/CORS）。
 *  /login 无需鉴权即返回 200；任何 HTTP 响应都代表后端可达。用于「后端未起→自动重连」。 */
ipcMain.handle("desktop:backend-health", async () => {
  const { base_url } = config.backend || {};
  if (!base_url) return { ok: false, error: "no base_url" };
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 2500);
  try {
    const r = await fetch(`${base_url}/login`, { method: "GET", redirect: "manual", signal: ctrl.signal });
    return { ok: true, status: r.status };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  } finally {
    clearTimeout(timer);
  }
});

ipcMain.handle("desktop:copy", (_e, text) => {
  try {
    clipboard.writeText(String(text || ""));
    return true;
  } catch (e) {
    return false;
  }
});

ipcMain.handle("desktop:diag", (_e, msg) => {
  console.log(`[inject] ${msg}`);
  return true;
});

// 选择器覆写层（D1 热更新）：注入脚本启动时拉取后端下发的选择器修正。
// 官方改版导致选择器失配时，运营改 config/desktop_selector_profiles.json 即可热修，
// 无需重发桌面包。后端不可达/无覆写时返回 {ok:true, profiles:{}}，注入静默用内置档。
ipcMain.handle("desktop:selector-profiles", async () => {
  try {
    return await backendGet("/api/desktop/selector-profiles");
  } catch (e) {
    return { ok: false, profiles: {}, error: String(e) };
  }
});

// 注入健康信标（D1b）：注入脚本把「逐选择器命中」状态上报后端，供运营看板判断
// 哪个账号/平台的注入因官方改版失配（而非笼统「坏了」）。后端不可达静默忽略。
ipcMain.handle("desktop:inject-health", async (_e, payload) => {
  try {
    return await backendPost("/api/desktop/inject-health", payload || {});
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:translate", async (_e, { text, target_lang }) => {
  try {
    return { ok: true, text: await backendTranslate(String(text || ""), target_lang) };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:translate-media", async (_e, { kind, b64, target_lang }) => {
  try {
    return await backendTranslateMedia(kind === "image" ? "image" : "voice", String(b64 || ""), target_lang);
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:smart-reply", async (_e, payload) => {
  try {
    return await backendSmartReply(payload || {});
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:ingest", async (_e, payload) => {
  try {
    return await backendIngest(payload || {});
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

/** 通用 DELETE（Bearer）。 */
async function backendDelete(pathname, query) {
  const { base_url, token } = config.backend || {};
  const qs = query
    ? "?" + new URLSearchParams(Object.entries(query).filter(([, v]) => v != null && v !== "")).toString()
    : "";
  const r = await fetch(`${base_url}${pathname}${qs}`, {
    method: "DELETE",
    headers: { Authorization: `Bearer ${token}` },
  });
  try { return await r.json(); } catch (e) { return { ok: false, status: r.status }; }
}

// ── 语音克隆 / TTS / 发送（与统一收件箱同源 API）──────────────────────────────
ipcMain.handle("desktop:voice-profiles", async () => {
  try { return await backendGet("/api/voice/profiles"); }
  catch (e) { return { ok: false, error: String(e) }; }
});

ipcMain.handle("desktop:voice-tts", async (_e, { text, persona_id }) => {
  try {
    const d = await backendPost("/api/voice/tts-test", { text, persona_id: persona_id || undefined });
    if (d.audio_url) return { ...d, ok: d.ok !== false };
    if (!d.filename) return d;
    const { base_url, token } = config.backend || {};
    const r = await fetch(
      `${base_url}/api/voice/tts-file/${encodeURIComponent(d.filename)}`,
      { headers: { Authorization: `Bearer ${token}` } });
    if (!r.ok) return { ok: false, message: `音频拉取失败 ${r.status}` };
    const b64 = Buffer.from(await r.arrayBuffer()).toString("base64");
    const mt = String(d.format || "mp3").includes("ogg") ? "audio/ogg" : "audio/mpeg";
    return { ...d, ok: true, dataUrl: `data:${mt};base64,${b64}` };
  } catch (e) { return { ok: false, error: String(e) }; }
});

ipcMain.handle("desktop:send-voice", async (_e, body) => {
  try { return await backendPost("/api/unified-inbox/send-voice", body || {}); }
  catch (e) { return { ok: false, error: String(e) }; }
});

ipcMain.handle("desktop:voice-reconcile", async () => {
  try { return await backendGet("/api/voice/reconcile"); }
  catch (e) { return { ok: false, error: String(e) }; }
});

ipcMain.handle("desktop:voice-purge", async (_e, body) => {
  try { return await backendPost("/api/voice/purge", body || {}); }
  catch (e) { return { ok: false, error: String(e) }; }
});

ipcMain.handle("desktop:voice-purge-orphans", async () => {
  try { return await backendPost("/api/voice/purge-orphans", {}); }
  catch (e) { return { ok: false, error: String(e) }; }
});

ipcMain.handle("desktop:voice-unbind", async (_e, { persona_id, purge_cloud }) => {
  try {
    const q = purge_cloud ? { purge_cloud: "1" } : {};
    return await backendDelete(`/api/voice/profiles/${encodeURIComponent(persona_id || "")}`, q);
  } catch (e) { return { ok: false, error: String(e) }; }
});

ipcMain.handle("desktop:voice-rebind", async (_e, body) => {
  try { return await backendPost("/api/voice/rebind", body || {}); }
  catch (e) { return { ok: false, error: String(e) }; }
});

ipcMain.handle("desktop:voice-enroll", async (_e, payload) => {
  try {
    const p = payload || {};
    const { base_url, token } = config.backend || {};
    const buf = Buffer.from(String(p.audio_b64 || ""), "base64");
    if (!buf.length) return { ok: false, message: "空音频" };
    const fd = new FormData();
    fd.append("file", new Blob([buf]), String(p.filename || "voice.wav"));
    fd.append("persona_id", String(p.persona_id || ""));
    fd.append("preferred_name", String(p.preferred_name || ""));
    fd.append("language_type", String(p.language_type || "Japanese"));
    if (p.reference_text) fd.append("reference_text", String(p.reference_text));
    const r = await fetch(`${base_url}/api/voice/enroll`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      body: fd,
    });
    return await r.json();
  } catch (e) { return { ok: false, error: String(e) }; }
});

// ── P2 业务右栏：复用后端统一收件箱 API ──────────────────────────────────────
ipcMain.handle("desktop:profile", async (_e, { platform, account_id, chat_key }) => {
  try {
    return await backendGet("/api/unified-inbox/profile", { platform, account_id, chat_key });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:kb-search", async (_e, { q, platform, intent }) => {
  try {
    return await backendGet("/api/unified-inbox/kb-search", { q, platform, intent, limit: 6 });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:templates", async () => {
  try {
    return await backendGet("/api/unified-inbox/templates");
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:personas", async () => {
  try {
    return await backendGet("/api/personas/profiles");
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

/** 通用后端 POST（主进程发起，规避 CSP）。 */
async function backendPost(pathname, body) {
  const { base_url, token } = config.backend || {};
  const r = await fetch(`${base_url}${pathname}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify(body || {}),
  });
  return await r.json();
}

ipcMain.handle("desktop:persona-bindings", async () => {
  try {
    return await backendGet("/api/persona/bindings");
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:persona-bind", async (_e, { chat_id, persona }) => {
  try {
    return await backendPost("/api/persona/bind", { chat_id, persona });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:guard-check", async (_e, { text }) => {
  try {
    return await backendPost("/api/desktop/guard-check", { text });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:persona-unbind", async (_e, { chat_id }) => {
  try {
    return await backendPost("/api/persona/unbind", { chat_id });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:thread", async (_e, { platform, account_id, chat_key }) => {
  try {
    return await backendGet("/api/unified-inbox/thread", { platform, account_id, chat_key, limit: 100 });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

// P1 共享组件:关系阶段(conv 级端点,conversation_id 由 renderer 本地拼)
ipcMain.handle("desktop:rel-stage", async (_e, { conversation_id }) => {
  try {
    const cid = String(conversation_id || "");
    if (!cid) return { ok: false, error: "missing conversation_id" };
    return await backendGet(`/api/workspace/conv/${encodeURIComponent(cid)}/relationship-stage`);
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:rel-confirm", async (_e, { conversation_id }) => {
  try {
    return await backendPost(`/api/workspace/conv/${encodeURIComponent(String(conversation_id || ""))}/relationship-stage/confirm`, {});
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:rel-downgrade", async (_e, { conversation_id, reason }) => {
  try {
    return await backendPost(`/api/workspace/conv/${encodeURIComponent(String(conversation_id || ""))}/relationship-stage/downgrade`, { reason: reason || "" });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:rel-reunion", async (_e, { conversation_id }) => {
  try {
    return await backendPost(`/api/workspace/conv/${encodeURIComponent(String(conversation_id || ""))}/relationship-stage/reunion`, {});
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:rel-sync", async (_e, { contact_id, mode }) => {
  try {
    return await backendPost(`/api/workspace/contact/${encodeURIComponent(String(contact_id || ""))}/relationship-stage/sync`, { mode: mode || "to_contact" });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

// P2 共享组件:NBA / 剧本话题(conv 级端点)
ipcMain.handle("desktop:nba-list", async (_e, { conversation_id }) => {
  try {
    const cid = String(conversation_id || "");
    if (!cid) return { ok: false, error: "missing conversation_id" };
    return await backendGet(`/api/workspace/conv/${encodeURIComponent(cid)}/next-actions`);
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:nba-exec", async (_e, { conversation_id, action_id, action_type, config }) => {
  try {
    return await backendPost(`/api/workspace/conv/${encodeURIComponent(String(conversation_id || ""))}/execute-action`, {
      action_id: action_id || "", action_type: action_type || "", config: config || {},
    });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:script-list", async (_e, { conversation_id }) => {
  try {
    const cid = String(conversation_id || "");
    if (!cid) return { ok: false, error: "missing conversation_id" };
    return await backendGet(`/api/workspace/conv/${encodeURIComponent(cid)}/script-suggestions`);
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:start-chain", async (_e, { conversation_id, chain_id }) => {
  try {
    return await backendPost(`/api/workspace/conv/${encodeURIComponent(String(conversation_id || ""))}/start-chain`, { chain_id: chain_id || "" });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

// P2 共享组件:协作上下文 / 工作链执行(conv 级端点)
ipcMain.handle("desktop:collab-context", async (_e, { conversation_id }) => {
  try {
    const cid = String(conversation_id || "");
    if (!cid) return { ok: false, error: "missing conversation_id" };
    return await backendGet(`/api/workspace/conv/${encodeURIComponent(cid)}/collab-context`);
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:chain-executions", async (_e, { conversation_id, limit }) => {
  try {
    const cid = String(conversation_id || "");
    if (!cid) return { ok: false, error: "missing conversation_id" };
    return await backendGet(`/api/workspace/conv/${encodeURIComponent(cid)}/chain-executions`, { limit: limit || 8 });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:chain-cancel", async (_e, { exec_id }) => {
  try {
    return await backendPost(`/api/workspace/chain-executions/${encodeURIComponent(String(exec_id || ""))}/cancel`, {});
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

// Phase 2 账号管理:统一清单 + 扫码登录 + 编排器启停（与 web 后台共用后端接口）
ipcMain.handle("desktop:accounts-list", async () => {
  try {
    return await backendGet("/api/accounts");
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:platform-modes", async (_e, { platform }) => {
  try {
    return await backendGet(`/api/platforms/${encodeURIComponent(String(platform || ""))}/modes`);
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:login-start", async (_e, args) => {
  try {
    const a = args || {};
    return await backendPost(`/api/platforms/${encodeURIComponent(String(a.platform || ""))}/login/start`, {
      mode: a.mode || "",
      account_id: a.account_id || "",
      label: a.label || "",
      proxy_id: a.proxy_id || "",
      use_fingerprint: !!a.use_fingerprint,
    });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:login-status", async (_e, { platform, login_id }) => {
  try {
    return await backendGet(
      `/api/platforms/${encodeURIComponent(String(platform || ""))}/login/${encodeURIComponent(String(login_id || ""))}/status`
    );
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:login-cancel", async (_e, { platform, login_id }) => {
  try {
    return await backendPost(
      `/api/platforms/${encodeURIComponent(String(platform || ""))}/login/${encodeURIComponent(String(login_id || ""))}/cancel`, {}
    );
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:account-start", async (_e, { platform, account_id }) => {
  try {
    return await backendPost(
      `/api/accounts/${encodeURIComponent(String(platform || ""))}/${encodeURIComponent(String(account_id || ""))}/start`, {}
    );
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:account-stop", async (_e, { platform, account_id }) => {
  try {
    return await backendPost(
      `/api/accounts/${encodeURIComponent(String(platform || ""))}/${encodeURIComponent(String(account_id || ""))}/stop`, {}
    );
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:account-auto-reply", async (_e, { platform, account_id, enabled }) => {
  try {
    return await backendPost(
      `/api/accounts/${encodeURIComponent(String(platform || ""))}/${encodeURIComponent(String(account_id || ""))}/auto-reply`,
      { enabled: !!enabled }
    );
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:account-auto-reply-override", async (_e, { platform, account_id, override }) => {
  try {
    return await backendPost(
      `/api/accounts/${encodeURIComponent(String(platform || ""))}/${encodeURIComponent(String(account_id || ""))}/auto-reply/override`,
      override || {}
    );
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:auto-reply-audit", async (_e, args) => {
  try {
    const { limit, platform, account_id, since } = args || {};
    return await backendGet("/api/accounts/auto-reply/audit", { limit, platform, account_id, since });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:auto-reply-config-get", async () => {
  try {
    return await backendGet("/api/accounts/auto-reply/config");
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:auto-reply-health", async () => {
  try {
    return await backendGet("/api/accounts/auto-reply/health");
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:auto-reply-webhooks-get", async () => {
  try {
    return await backendGet("/api/accounts/auto-reply/webhooks");
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:auto-reply-webhooks-set", async (_e, list) => {
  try {
    return await backendPost("/api/accounts/auto-reply/webhooks", { webhooks: list || [] });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:auto-reply-webhooks-test", async (_e, payload) => {
  try {
    return await backendPost("/api/accounts/auto-reply/webhooks/test", payload || {});
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:auto-reply-config-set", async (_e, args) => {
  try {
    return await backendPost("/api/accounts/auto-reply/config", args || {});
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:analyze", async (_e, { messages, chat }) => {
  try {
    const { base_url, token } = config.backend || {};
    const r = await fetch(`${base_url}/api/unified-inbox/analyze`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify({ messages: Array.isArray(messages) ? messages : [], chat: chat || {} }),
    });
    return await r.json();
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

/** 给某账号的 session 分区配置代理（防关联，可选）。 */
async function applyProxyForAccount(acc) {
  if (!acc || !acc.proxy) return;
  try {
    const part = `persist:${acc.id}`;
    await session.fromPartition(part).setProxy({ proxyRules: acc.proxy });
  } catch (e) {
    // 代理配置失败不阻断启动
  }
}

/** WhatsApp/Instagram/Messenger/X/Zalo 等拒载含 Electron 的 UA；在 partition 级伪装 Chrome
 *  （须在首次导航前）。telegram 用默认 UA 已验证可用 → 跳过，保持零回归。 */
async function applyWhatsappSessionUa(acc) {
  if (!acc || !needsChromeUa(acc.platform)) return;
  try {
    await session.fromPartition(`persist:${acc.id}`).setUserAgent(
      chromeLikeUserAgent(process.versions.chrome));
  } catch (e) {
    // UA 设置失败不阻断启动
  }
}

function bindWhatsappWebviewUa(wc) {
  const waUa = chromeLikeUserAgent(process.versions.chrome);
  function maybeSet(url) {
    if (urlNeedsChromeUa(url)) {
      try { wc.setUserAgent(waUa); } catch (_) { /* ignore */ }
    }
  }
  maybeSet(wc.getURL());
  wc.on("did-start-navigation", (_e, url) => maybeSet(url));
  wc.on("will-navigate", (_e, url) => maybeSet(url));
}

async function createWindow() {
  for (const acc of config.accounts || []) {
    await applyProxyForAccount(acc);
    await applyWhatsappSessionUa(acc);
  }

  const win = new BrowserWindow({
    width: 1280,
    height: 820,
    title: "AI 客服桌面客户端（多平台）",
    webPreferences: {
      preload: path.join(__dirname, "shell-preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      webviewTag: true,
    },
  });

  win.webContents.on("did-finish-load", () => console.log("[diag] renderer loaded ok"));
  win.webContents.on("did-fail-load", (_e, code, desc) =>
    console.log(`[diag] renderer load FAILED ${code} ${desc}`));
  win.webContents.on("render-process-gone", (_e, d) =>
    console.log(`[diag] render process gone: ${JSON.stringify(d)}`));
  win.webContents.on("console-message", (_e, _lvl, msg) =>
    console.log(`[renderer] ${msg}`));
  // webview 子 webContents 在 Electron 默认是 sandboxed（与父窗口 sandbox:false 无关），
  // 沙箱内 preload 只能 require electron，无法 require 本地模块（./profiles.js / ./media-format.js）→
  // 注入脚本 tg-inject.js 整体加载失败「module not found: ./profiles.js」。这里对内嵌 webview 关闭
  // 沙箱，使 preload 能加载选择器档案/媒体格式化模块（DOM 注入与 ipcRenderer 不受影响）。
  win.webContents.on("will-attach-webview", (_e, webPreferences) => {
    webPreferences.sandbox = false;
  });
  win.webContents.on("did-attach-webview", (_e, wc) => {
    console.log("[diag] webview attached");
    bindWhatsappWebviewUa(wc);
    wc.on("did-finish-load", () => console.log("[diag] webview page loaded"));
    wc.on("did-fail-load", (_e2, code, desc) =>
      console.log(`[diag] webview load FAILED ${code} ${desc}`));
    wc.on("console-message", (_e2, _lvl, msg) =>
      console.log(`[webview] ${msg}`));
  });

  console.log(`[diag] platforms enabled: ${(config.platforms || []).filter((p) => p.enabled).map((p) => p.id).join(",")}`);
  win.loadFile(path.join(__dirname, "renderer", "index.html"));
  if (process.argv.includes("--dev")) win.webContents.openDevTools({ mode: "detach" });
}

// 中文应用菜单(替换默认英文菜单;保留 role 以维持快捷键与原生行为)
function buildChineseMenu() {
  const template = [
    {
      label: "文件",
      submenu: [
        { label: "重新加载", role: "reload" },
        { label: "强制重新加载", role: "forceReload" },
        { type: "separator" },
        { label: "退出", role: "quit" },
      ],
    },
    {
      label: "编辑",
      submenu: [
        { label: "撤销", role: "undo" },
        { label: "重做", role: "redo" },
        { type: "separator" },
        { label: "剪切", role: "cut" },
        { label: "复制", role: "copy" },
        { label: "粘贴", role: "paste" },
        { label: "全选", role: "selectAll" },
      ],
    },
    {
      label: "视图",
      submenu: [
        { label: "实际大小", role: "resetZoom" },
        { label: "放大", role: "zoomIn" },
        { label: "缩小", role: "zoomOut" },
        { type: "separator" },
        { label: "全屏", role: "togglefullscreen" },
        { label: "开发者工具", role: "toggleDevTools" },
      ],
    },
    {
      label: "窗口",
      submenu: [
        { label: "最小化", role: "minimize" },
        { label: "关闭窗口", role: "close" },
      ],
    },
    {
      label: "帮助",
      submenu: [
        {
          label: "关于",
          click: () => {
            const w = BrowserWindow.getFocusedWindow() || BrowserWindow.getAllWindows()[0];
            if (w) w.webContents.executeJavaScript(
              "alert('AI 客服桌面客户端（多平台）\\nTelegram / WhatsApp / Messenger / LINE · 统一收件箱 + 统一业务面板')"
            );
          },
        },
      ],
    },
  ];
  return Menu.buildFromTemplate(template);
}

app.whenReady().then(() => {
  Menu.setApplicationMenu(buildChineseMenu());
  createWindow();
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});
