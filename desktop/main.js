"use strict";

const { app, BrowserWindow, ipcMain, session, clipboard, Menu, Notification, shell, dialog, nativeImage } = require("electron");
const path = require("path");
const fs = require("fs");
const { spawn, exec } = require("child_process");
const { chromeLikeUserAgent, isWhatsappUrl, needsChromeUa, urlNeedsChromeUa } = require("./webview-ua.js");
const { fingerprintArg, accountIdFromPartition } = require("./inject/fingerprint.js");
const { createBackendManager } = require("./backend-launcher.js");
const brandUtil = require("./brand-util.js");

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
      backend: { base_url: "http://127.0.0.1:18799", token: "admin" },
      translate: { target_lang: "zh", auto: false },
      platforms: [],
      accounts: [],
    };
  }
}

let config = loadConfig();

// 内置默认品牌图标（copy-shared 落地）。窗口创建时用它作原生图标兜底，
// 白标改回默认时也还原到它。
const DEFAULT_BRAND_ICON = path.join(__dirname, "renderer", "brand", "boundless-mark-256.png");

// 品牌信息统一形状：{ product, company, website, mark }（纯逻辑见 brand-util.js）。
// 离线兜底：config.brand → copy-shared 落地的 renderer/brand/brand.json → 硬编码默认。
function brandInfoLocal() {
  let brandJson = null;
  try {
    brandJson = JSON.parse(
      fs.readFileSync(path.join(__dirname, "renderer", "brand", "brand.json"), "utf-8"));
  } catch (e) { /* 无 brand.json → 走硬编码兜底 */ }
  return brandUtil.pickBrandLocal((config && config.brand) || {}, brandJson);
}

// 运行期白标：向后端拉实时生效品牌（settings 页改了即时反映到桌面壳），
// 2s 超时 + 任何异常回落本地——绝不阻断桌面壳。
async function fetchLiveBrand() {
  const { base_url, token } = config.backend || {};
  if (!base_url) return null;
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 2000);
  try {
    const r = await fetch(`${base_url}/api/admin/branding`, {
      headers: { Authorization: `Bearer ${token || ""}` },
      signal: ctl.signal,
    });
    if (!r.ok) return null;
    return brandUtil.normalizeLiveBrand(await r.json());
  } catch (e) {
    return null; // 后端未起 / 超时 / 无授权 → 用本地兜底
  } finally {
    clearTimeout(timer);
  }
}

// 实时优先、本地兜底的统一入口（关于弹窗 / 图标用）。
async function resolveBrand() {
  return (await fetchLiveBrand()) || brandInfoLocal();
}

// macOS dock 图标运行期热替换（Win/Linux 无 app.dock，静默跳过）。
function _setDockIcon(img) {
  try {
    if (process.platform === "darwin" && app.dock && img && !img.isEmpty()) {
      app.dock.setIcon(img);
    }
  } catch (e) { /* dock 图标设置失败不影响使用 */ }
}

// 运行期把生效品牌 logo 同步成原生窗口/任务栏图标——settings 页改了 logo，
// 桌面壳窗口 focus 时（节流后）重取并热替换，无需重开窗口即时生效。
//   custom  : 白标自定义 logo（且与上次不同）→ 下载 + setIcon
//   default : 白标改回默认（上次是自定义）→ 还原内置图标
//   none    : 无变化 → 不动，避免无谓下载/闪烁
// 默认无界 mark 在窗口创建时已用本地文件设过；任何失败都保留现图标、不阻断。
async function applyLiveWindowBranding(win, { force = false } = {}) {
  try {
    if (!win || win.isDestroyed()) return;
    const now = Date.now();
    if (!force && !brandUtil.shouldCheckBrand(now, win._brandLastCheck)) return;
    win._brandLastCheck = now;
    const bi = await fetchLiveBrand();
    if (!bi) return;
    const act = brandUtil.resolveIconAction(bi.mark, win._brandMark);
    if (act.action === "custom") {
      const url = brandUtil.resolveBackendUrl((config.backend || {}).base_url, act.mark);
      if (!url) return;
      const r = await fetch(url);
      if (!r.ok) return;
      const img = nativeImage.createFromBuffer(Buffer.from(await r.arrayBuffer()));
      if (!img.isEmpty() && !win.isDestroyed()) {
        win.setIcon(img);
        _setDockIcon(img);
        win._brandMark = act.mark;
      }
    } else if (act.action === "default") {
      if (fs.existsSync(DEFAULT_BRAND_ICON) && !win.isDestroyed()) {
        const img = nativeImage.createFromPath(DEFAULT_BRAND_ICON);
        win.setIcon(img);
        _setDockIcon(img);
        win._brandMark = null;
      }
    }
  } catch (e) { /* 图标热替换失败不影响使用 */ }
}

// 后端 sidecar 生命周期：免手动起 Python。拉起前先探活（已在跑→复用），退出回收。
const backendManager = createBackendManager({ app, spawn, exec, fs, fetch: global.fetch });

/** 浅合并并持久化 config.json（首启向导用）；同步更新内存 config。返回 {ok}。 */
function saveConfigPatch(patch) {
  try {
    const next = Object.assign({}, config);
    for (const [k, v] of Object.entries(patch || {})) {
      next[k] = (v && typeof v === "object" && !Array.isArray(v))
        ? Object.assign({}, config[k] || {}, v) : v;
    }
    fs.writeFileSync(CONFIG_PATH, JSON.stringify(next, null, 2), "utf-8");
    config = next;
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
}

ipcMain.handle("desktop:save-config", (_e, patch) => saveConfigPatch(patch || {}));

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
// 后端拉起状态（idle/probing/starting/ready/running-external/failed/disabled/stopped）。
// renderer 可据此把「正在连接后台」细化为「正在启动后台服务…」并在 failed 时给指引。
ipcMain.handle("desktop:backend-spawn-status", () => backendManager.getStatus());

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

// 原生系统通知（新私聊消息弹窗，由工作台前端按用户选择的「弹窗方式」调用）。
// 点击通知 → 唤起并聚焦主窗口。系统不支持/被禁用时返回 {ok:false}，前端回落浏览器通知/应用内提示。
ipcMain.handle("desktop:notify", (_e, args) => {
  try {
    const a = args || {};
    if (!Notification.isSupported()) return { ok: false, error: "unsupported" };
    const n = new Notification({
      title: String(a.title || "新消息"),
      body: String(a.body || ""),
      silent: a.silent === true,
    });
    n.on("click", () => {
      const w = BrowserWindow.getAllWindows()[0];
      if (w) { try { if (w.isMinimized()) w.restore(); w.show(); w.focus(); } catch (e) { /* ignore */ } }
    });
    n.show();
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
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

// 自动化健康看板（壳层聚合读）：把后端「全账号注入健康 + 持续失配」汇总下发给壳层 🩺 面板，
// 让运营在桌面壳内一眼看清各内嵌账号注入是否健康（而非只看当前聚焦 webview）。后端不可达返回空摘要。
ipcMain.handle("desktop:inject-health-list", async (_e, args) => {
  try {
    const persist_sec = (args && args.persist_sec) || undefined;
    return await backendGet("/api/desktop/inject-health", { persist_sec });
  } catch (e) {
    return { ok: false, summary: {}, accounts: [], error: String(e) };
  }
});

// 受控出站队列概览（D4b 壳层读）：pending/claimed/sent/failed 计数 + 近期命令预览，
// 让运营看清全自动回复经 send-gate/kill-switch 后是否在正常流转/有无卡死。后端不可达返回空摘要。
ipcMain.handle("desktop:outbound-stats", async (_e, args) => {
  try {
    const limit = (args && args.limit) || undefined;
    return await backendGet("/api/desktop/outbound/stats", { limit });
  } catch (e) {
    return { ok: false, summary: {}, recent: [], error: String(e) };
  }
});

// 注入「持续失配」告警流（壳层 🩺 红点预警 + 面板告警块用）：只有**连续**失配超阈值才进 alerts
// （即时抖动自愈、不误报）；events 为状态跃迁趋势。后端不可达返回空告警（红点不亮）。
ipcMain.handle("desktop:inject-alerts", async (_e, args) => {
  try {
    const persist_sec = (args && args.persist_sec) || undefined;
    const limit = (args && args.limit) || undefined;
    return await backendGet("/api/desktop/inject-health/alerts", { persist_sec, limit });
  } catch (e) {
    return { ok: false, alerts: [], events: [], error: String(e) };
  }
});

// D1 一键热修：向后端取「覆写文件本地路径」（不存在则首次写模板），用系统默认编辑器打开。
// 后端返回的路径与注入读取的 selector-profiles 同一文件（同 config 目录），避免「打开 A 却读 B」。
ipcMain.handle("desktop:open-selectors", async () => {
  try {
    const r = await backendGet("/api/desktop/selector-profiles/path");
    if (!r || !r.ok || !r.path) {
      return { ok: false, error: (r && r.error) || "后端未返回路径" };
    }
    const err = await shell.openPath(r.path); // 成功返回 ""，失败返回错误串
    if (err) return { ok: false, path: r.path, error: err };
    return { ok: true, path: r.path, created: !!r.created };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

// D1 校验：读覆写文件给运营显式反馈（解析失败/被忽略字段），与注入读取同一文件。
ipcMain.handle("desktop:validate-selectors", async () => {
  try {
    return await backendGet("/api/desktop/selector-profiles/validate");
  } catch (e) {
    return { ok: false, valid: false, error: String(e), profiles: 0, platforms: [], dropped: [] };
  }
});

// D4 受控出站桥：轮询「受控出站队列」取走发给本内嵌账号的全自动回复（已先过后端
// send-gate/kill-switch 闸门），renderer 据此调 webview fill-composer 在官方页 DOM 发送，
// 再回执。后端不可达静默忽略（autopilot 命令仍留在队列，下轮重取）。
ipcMain.handle("desktop:outbound-pull", async (_e, { platform, account_id, limit }) => {
  try {
    return await backendGet("/api/desktop/outbound", { platform, account_id, limit: limit || 20 });
  } catch (e) {
    return { ok: false, items: [], error: String(e) };
  }
});

ipcMain.handle("desktop:outbound-ack", async (_e, { id, ok, error }) => {
  try {
    return await backendPost("/api/desktop/outbound/ack", { id, ok: ok !== false, error: error || "" });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

// 受控出站「人审介入」（P2）：拦截/暂停/放行/改写/重试某条命令。
ipcMain.handle("desktop:outbound-action", async (_e, { id, ids, action, text, reason, ai_suggestion, source }) => {
  try {
    return await backendPost("/api/desktop/outbound/action", {
      id, ids, action, text: text || "", reason: reason || "",
      ai_suggestion: ai_suggestion || "", source: source || "",
    });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

// AI 重写助手（P4.1）：给一条命令生成更好的候选回复（不落库，供人审采纳）。
ipcMain.handle("desktop:outbound-rewrite", async (_e, { id }) => {
  try {
    return await backendPost("/api/desktop/outbound/rewrite", { id });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

// 纠正样本导出（P5）：拉 JSONL（偏好对）→ 保存对话框写文件，供离线 fine-tune/eval。
ipcMain.handle("desktop:export-corrections", async (_e, opts) => {
  try {
    const { base_url, token } = config.backend || {};
    if (!base_url) return { ok: false, error: "no base_url" };
    const q = new URLSearchParams({ format: "jsonl", limit: "5000" });
    if (opts && opts.source) q.set("source", opts.source);
    if (opts && opts.kind) q.set("kind", opts.kind);
    const r = await fetch(`${base_url}/api/desktop/outbound/corrections?${q.toString()}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    const text = await r.text();
    const count = text ? text.split("\n").filter(Boolean).length : 0;
    if (!count) return { ok: false, error: "无样本可导出" };
    const win = BrowserWindow.getFocusedWindow();
    const def = `desktop_corrections_${new Date().toISOString().slice(0, 10)}.jsonl`;
    const res = await dialog.showSaveDialog(win, {
      defaultPath: def,
      filters: [{ name: "JSONL", extensions: ["jsonl"] }],
    });
    if (res.canceled || !res.filePath) return { ok: false, canceled: true };
    fs.writeFileSync(res.filePath, text, "utf-8");
    return { ok: true, path: res.filePath, count };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
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

ipcMain.handle("desktop:set-title", (_e, title) => {
  const w = BrowserWindow.getFocusedWindow() || BrowserWindow.getAllWindows()[0];
  if (w && title) {
    try { w.setTitle(String(title)); } catch (e) { /* ignore */ }
  }
  return { ok: true };
});

async function createWindow() {
  for (const acc of config.accounts || []) {
    await applyProxyForAccount(acc);
    await applyWhatsappSessionUa(acc);
  }

  // 原生窗口/任务栏图标兜底：内嵌 webview 报告的 page-favicon 不会传染给外层窗口，
  // 故显式用 copy-shared 落地的品牌 mark，保证桌面壳始终是无界图标（缺文件则回落 Electron 默认）。
  const winOpts = {
    width: 1280,
    height: 820,
    title: "智聊 · 桌面工作台",
    webPreferences: {
      preload: path.join(__dirname, "shell-preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      webviewTag: true,
    },
  };
  try {
    if (fs.existsSync(DEFAULT_BRAND_ICON)) winOpts.icon = DEFAULT_BRAND_ICON;
  } catch (e) { /* 图标缺失不阻断启动 */ }
  const win = new BrowserWindow(winOpts);

  win.webContents.on("did-finish-load", () => {
    console.log("[diag] renderer loaded ok");
    applyLiveWindowBranding(win, { force: true });
  });
  // 运行中改了 logo 无需重开窗口：切回桌面壳时（节流后）重取品牌热替换图标。
  win.on("focus", () => applyLiveWindowBranding(win));
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
    wc.on("console-message", (_e2, _lvl, msg, line, sourceId) =>
      console.log(`[webview] ${msg}${sourceId ? ` (${sourceId}:${line})` : ""}`));
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
          click: async () => {
            const w = BrowserWindow.getFocusedWindow() || BrowserWindow.getAllWindows()[0];
            const bi = await resolveBrand();
            const ver = app.getVersion();
            const detail =
              `Telegram / WhatsApp / Messenger / LINE · 统一收件箱 + 业务助手\n\n` +
              `版本 v${ver}  ·  Electron ${process.versions.electron}  ·  Chromium ${process.versions.chrome}\n` +
              `${bi.company} · ${bi.website}`;
            const res = await dialog.showMessageBox(w, {
              type: "info",
              title: `关于 ${bi.product}`,
              message: `${bi.product} · 桌面工作台`,
              detail,
              buttons: ["访问官网", "关闭"],
              defaultId: 1,
              cancelId: 1,
              noLink: true,
            });
            if (res.response === 0 && bi.website) {
              shell.openExternal(bi.website).catch(() => {});
            }
          },
        },
        {
          label: "检查更新",
          click: () => checkForUpdatesManual(
            BrowserWindow.getFocusedWindow() || BrowserWindow.getAllWindows()[0]),
        },
      ],
    },
  ];
  return Menu.buildFromTemplate(template);
}

/** 手动「检查更新」：dev 说明不检查；发布态调 electron-updater 并给出可读反馈。
 *  与后台自动更新共用 autoDownload=true——发现新版即后台下载、下次重启生效。 */
async function checkForUpdatesManual(win) {
  const ver = app.getVersion();
  if (!app.isPackaged) {
    await dialog.showMessageBox(win, {
      type: "info", title: "检查更新",
      message: "当前为开发版", detail: `版本 v${ver}（开发模式不检查更新）`,
      buttons: ["好的"], noLink: true,
    });
    return;
  }
  let autoUpdater;
  try {
    ({ autoUpdater } = require("electron-updater"));
  } catch (e) {
    await dialog.showMessageBox(win, {
      type: "error", title: "检查更新", message: "更新组件不可用",
      detail: String((e && e.message) || e), buttons: ["好的"], noLink: true,
    });
    return;
  }
  try {
    autoUpdater.autoDownload = true;
    const r = await autoUpdater.checkForUpdates();
    const latest = r && r.updateInfo && r.updateInfo.version;
    if (latest && latest !== ver) {
      await dialog.showMessageBox(win, {
        type: "info", title: "检查更新", message: `发现新版本 v${latest}`,
        detail: "更新正在后台下载，下次重启自动生效。", buttons: ["好的"], noLink: true,
      });
    } else {
      await dialog.showMessageBox(win, {
        type: "info", title: "检查更新", message: "已是最新版本",
        detail: `当前 v${ver}`, buttons: ["好的"], noLink: true,
      });
    }
  } catch (e) {
    await dialog.showMessageBox(win, {
      type: "error", title: "检查更新", message: "检查更新失败",
      detail: String((e && e.message) || e), buttons: ["好的"], noLink: true,
    });
  }
}

/** 自动更新（仅发布态；dev 跳过。失败不阻断启动）。需 package.json::build.publish 指向真实更新源。 */
function setupAutoUpdate() {
  if (!app.isPackaged) return;
  try {
    const { autoUpdater } = require("electron-updater");
    autoUpdater.autoDownload = true;
    autoUpdater.on("error", (e) => console.log(`[updater] ${String((e && e.message) || e)}`));
    autoUpdater.on("update-downloaded", () => console.log("[updater] 更新已下载，下次重启生效"));
    autoUpdater.checkForUpdatesAndNotify().catch((e) =>
      console.log(`[updater] check failed: ${String((e && e.message) || e)}`));
  } catch (e) {
    console.log(`[updater] 不可用：${String((e && e.message) || e)}`);
  }
}

// 单实例锁（双实例竞态根治）：多开桌面壳会各自 backendManager.start() → 各自探活后
// 各自 spawn 后端，端口先到者赢、后到者绑定失败成僵尸实例（曾观测到双 python main.py）。
// 第二个实例直接退出，并把已有窗口唤到前台（符合「再次启动=聚焦既有窗口」的预期）。
const _gotSingleInstanceLock = app.requestSingleInstanceLock();
if (!_gotSingleInstanceLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    const w = BrowserWindow.getAllWindows()[0];
    if (w) {
      try { if (w.isMinimized()) w.restore(); w.show(); w.focus(); } catch (e) { /* ignore */ }
    }
  });

  app.whenReady().then(() => {
    Menu.setApplicationMenu(buildChineseMenu());
    // 后台自拉起（不阻塞开窗：renderer 已有「正在连接后台→自动重连」遮罩兜底）。
    backendManager.start(config).catch((e) => console.log(`[backend] start error: ${e}`));
    createWindow();
    setupAutoUpdate();
  });
}

// 退出时回收后端进程，避免残留 Python/二进制占端口。
let _backendStopped = false;
app.on("before-quit", () => {
  if (_backendStopped) return;
  _backendStopped = true;
  try { backendManager.stop(); } catch (e) { /* 回收失败不阻断退出 */ }
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});
