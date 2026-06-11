"use strict";

// 多平台内容脚本（历史文件名 tg-inject.js，现已平台无关）。
// 作为 webview 的 preload 运行：与页面共享 DOM，但跑在隔离世界，可用 ipcRenderer 调主进程。
// 主进程再去请求本仓库 FastAPI 后端（规避 webview 的跨域/混合内容限制）。
//
// 架构：按 location.hostname 探测平台 → 选用对应「选择器档案 PROFILE」。
//   - telegram（web.telegram.org/k，webk）：完整支持（翻译/同步/智能回复/填入发送）。
//   - whatsapp（web.whatsapp.com）：best-effort 档案（beta，类名随版本变，需现场用 F12 校准）。
//   - messenger / line / unknown：显式不支持 → 不挂任何 UI，也绝不把消息误标成 telegram。
//
// ⚠️ 各平台类名会随官方改版变化。若按钮没出现/抓不到文本，按对应 PROFILE 注释调整（F12 看真实 DOM）。

const { ipcRenderer } = require("electron");
// 媒体翻译结果格式化（纯函数，可单测）。require 失败时退化：不提供媒体翻译，不影响文本链路。
let formatMediaResult = null;
try {
  ({ formatMediaResult } = require("./media-format.js"));
} catch (e) {
  /* 媒体格式化模块缺失：媒体翻译降级关闭 */
}

// ── 通用文本清洗（跨平台共用）────────────────────────────────────────────────
function cleanVisibleText(raw) {
  let txt = String(raw || "");
  // 去图标字体私用区字形（已读勾/状态，E000–F8FF），否则夹进正文/时间戳
  txt = txt.replace(/[\uE000-\uF8FF]/g, "");
  // 兜底：剥掉尾部粘连且重复的时间戳，如「你好哦17:1017:10」
  txt = txt.replace(/(\d{1,2}:\d{2})\1+\s*$/, "");
  return txt.trim();
}

// ── 平台探测 ─────────────────────────────────────────────────────────────────
function detectPlatform() {
  const h = (location.hostname || "").toLowerCase();
  if (h.indexOf("web.telegram.org") >= 0) return "telegram";
  if (h.indexOf("web.whatsapp.com") >= 0) return "whatsapp";
  if (h.indexOf("messenger.com") >= 0 || h.indexOf("facebook.com") >= 0) return "messenger";
  if (h.indexOf("line.me") >= 0 || h.indexOf("line-apps.com") >= 0) return "line";
  return "unknown";
}

// ── 选择器档案 ───────────────────────────────────────────────────────────────
const PROFILES = {
  // Telegram webk —— 与原实现逐字等价，保持零回归。
  telegram: {
    platform: "telegram",
    supported: true,
    canIngest: true,
    richInput: false, // 用 textContent + input 事件（webk 已验证可行）
    bubble: ".bubble",
    bubbleText: ".message, .translatable-message",
    outFlag: "is-out",
    composer: ".input-message-input",
    sendBtn: ".btn-send",
    peerTitle:
      ".chat-info .peer-title, .topbar .peer-title, .chat .user-title, .sidebar-header .peer-title",
    isContent(b) {
      if (!b || !b.classList) return false;
      // 排除日期分隔/系统/服务/赞助气泡（否则「January 21」被当消息，污染上下文）
      return !(
        b.classList.contains("service") ||
        b.classList.contains("is-date") ||
        b.classList.contains("is-system") ||
        b.classList.contains("is-sponsored") ||
        b.classList.contains("bubble-first-unread")
      );
    },
    text(bubble) {
      // 文件/文档气泡：只取文件名，避免把尺寸/层级元信息抓成乱码
      const docName = bubble.querySelector(
        ".document-name, .document .name, .media-container .document-name"
      );
      if (docName && !bubble.querySelector(this.bubbleText)) {
        const name = (docName.textContent || "").replace(/\s+/g, " ").trim();
        return name ? "[文件] " + name : "[文件]";
      }
      const node = bubble.querySelector(this.bubbleText) || bubble;
      const clone = node.cloneNode(true);
      // 剔除：① 注入块/按钮 ② 时间戳/已读/反应 ③ 文档/语音/附件/引用容器
      clone
        .querySelectorAll(
          ".aitr-box,.aitr-btn,.time,.message-time,.reactions,.bubble-beside-button," +
            ".document,.audio,.attachment,.web,.reply"
        )
        .forEach((n) => n.remove());
      return cleanVisibleText(clone.textContent || "");
    },
    isOut(b) {
      return b.classList.contains(this.outFlag);
    },
    mid(bubble) {
      return (
        bubble.getAttribute("data-mid") ||
        bubble.getAttribute("data-message-id") ||
        (bubble.closest && bubble.closest("[data-mid]")
          ? bubble.closest("[data-mid]").getAttribute("data-mid")
          : "") ||
        ""
      );
    },
    peerId(bubble) {
      // 只用数字 peer-id，不回落 hash（hash 可能是 @username，混用会把会话拆成两条）
      const fromBubble = bubble.getAttribute("data-peer-id");
      if (fromBubble) return fromBubble;
      const container = bubble.closest && bubble.closest("[data-peer-id]");
      if (container) return container.getAttribute("data-peer-id") || "";
      return "";
    },
    peerName() {
      const el = document.querySelector(this.peerTitle);
      return el ? (el.textContent || "").trim() : "";
    },
    // 纯媒体气泡 → {kind:"image"|"voice", url}（图片 OCR / 语音转写翻译用）。取不到返回 null。
    media(bubble) {
      const img = bubble.querySelector(".media-photo, .media-container img, .attachment img");
      if (img && img.src && img.src.indexOf("data:") !== 0) return { kind: "image", url: img.src };
      const audio = bubble.querySelector("audio[src]");
      if (audio && audio.getAttribute("src")) return { kind: "voice", url: audio.src };
      return null;
    },
  },

  // WhatsApp Web —— best-effort（beta）。类名相对稳定，但官方改版可能失效。
  //   消息行：div.message-in / div.message-out；文本：span.selectable-text。
  //   会话/消息 id：最近的 [data-id]，格式 {fromMe}_{chatId}_{msgId}（chatId 形如 6591234567@c.us）。
  whatsapp: {
    platform: "whatsapp",
    supported: true,
    canIngest: true,
    richInput: true, // contenteditable + React/Lexical，需 execCommand insertText
    bubble: "div.message-in, div.message-out, div[data-id*='@c.us'], div[data-id*='@g.us']",
    // 多候选 + 多语言，抗官方改版（只增不删；新版 contenteditable 常带 role=textbox）
    composer:
      'footer div[contenteditable="true"][data-tab], div[contenteditable="true"][data-tab="10"], ' +
      'footer div[contenteditable="true"][role="textbox"], div[contenteditable="true"][aria-label]',
    sendBtn:
      'button[aria-label="发送"], button[aria-label="Send"], button[data-testid="compose-btn-send"], ' +
      'span[data-icon="send"], span[data-icon="send-light"], span[data-icon="wds-ic-send-filled"]',
    isContent(b) {
      if (!b || !b.getAttribute) return false;
      if (b.classList && (b.classList.contains("message-in") || b.classList.contains("message-out"))) {
        return true;
      }
      const did = b.getAttribute("data-id") || "";
      return did.indexOf("@c.us") >= 0 || did.indexOf("@g.us") >= 0;
    },
    _dataId(bubble) {
      // data-id 常在子节点上（不在 message-in/out 根上），仅 closest 会漏
      const inner = bubble.querySelector && bubble.querySelector("[data-id]");
      if (inner) return inner.getAttribute("data-id") || "";
      const host = bubble.closest && bubble.closest("[data-id]");
      return (host && host.getAttribute("data-id")) || "";
    },
    text(bubble) {
      const span = bubble.querySelector(
        "span.selectable-text, .copyable-text span.selectable-text, [data-testid='selectable-text']"
      );
      let raw = span ? span.textContent : "";
      if (!raw) {
        const cp = bubble.querySelector(".copyable-text, .copyable-area");
        raw = cp ? cp.textContent : "";
      }
      if (!raw && bubble.getAttribute && bubble.getAttribute("data-id")) {
        raw = bubble.textContent || "";
      }
      return cleanVisibleText(raw);
    },
    isOut(b) {
      return b.classList.contains("message-out");
    },
    mid(bubble) {
      const parts = this._dataId(bubble).split("_");
      return parts.length >= 3 ? parts[parts.length - 1] : "";
    },
    peerId(bubble) {
      const parts = this._dataId(bubble).split("_");
      if (parts.length >= 3 && parts[1].indexOf("@") >= 0) return parts[1];
      return this.conversationPeerId ? this.conversationPeerId() : "";
    },
    peerName() {
      const el =
        document.querySelector('header span[dir="auto"][title]') ||
        document.querySelector('header [data-testid="conversation-info-header-chat-title"]') ||
        document.querySelector('header span[dir="auto"]');
      return el ? (el.getAttribute("title") || el.textContent || "").trim() : "";
    },
    conversationPeerId() {
      // 消息行尚未带 data-id 时，从会话主面板/header 回落 chat jid
      const sel =
        '#main [data-id*="@c.us"], #main [data-id*="@g.us"], ' +
        'header [data-id*="@c.us"], header [data-id*="@g.us"]';
      const el = document.querySelector(sel);
      if (!el) return "";
      const parts = (el.getAttribute("data-id") || "").split("_");
      if (parts.length >= 2 && parts[1].indexOf("@") >= 0) return parts[1];
      return "";
    },
    // 纯媒体气泡 → {kind, url}。WhatsApp 图片/语音多为 blob: URL（同源可 fetch）。
    media(bubble) {
      const img = bubble.querySelector('img[src^="blob:"], img[src^="http"]');
      if (img && img.src) return { kind: "image", url: img.src };
      const audio = bubble.querySelector("audio[src]");
      if (audio && audio.getAttribute("src")) return { kind: "voice", url: audio.src };
      return null;
    },
  },
};

const PLATFORM = detectPlatform();
const PROFILE = PROFILES[PLATFORM] || { platform: PLATFORM, supported: false };

const PUSHED = new Set(); // 同步桥去重：peerId:mid

let CONFIG = { translate: { target_lang: "zh", auto: false } };
let CURRENT_PERSONA = ""; // 由壳层右栏下发；空=用账号/domain 默认人设
let REPLY_LANG = ""; // 由壳层右栏下发：浮钮智能回复的目标语言（空=跟随人设/客户）
let ACCOUNT_ID = ""; // 由壳层 renderer 下发：本 webview 归属账号（多账号下同平台 hostname 相同，inject 自己分不清）

const PROCESSED = "data-aitr"; // 标记已处理，避免重复注入

function targetLang() {
  return (CONFIG.translate && CONFIG.translate.target_lang) || "zh";
}

function bubbleVisibleText(bubble) {
  try {
    return PROFILE.text ? PROFILE.text(bubble) : "";
  } catch (e) {
    return "";
  }
}

function makeBtn(label) {
  const b = document.createElement("span");
  b.className = "aitr-btn";
  b.textContent = label;
  b.style.cssText =
    "display:inline-block;margin-top:4px;padding:1px 8px;font-size:11px;cursor:pointer;" +
    "border-radius:10px;background:rgba(80,160,255,.18);color:#3aa0ff;user-select:none;";
  return b;
}

function renderTranslation(bubble, text) {
  let box = bubble.querySelector(".aitr-box");
  if (!box) {
    box = document.createElement("div");
    box.className = "aitr-box";
    box.style.cssText =
      "margin-top:4px;padding:4px 8px;font-size:13px;line-height:1.4;white-space:pre-wrap;" +
      "border-left:2px solid #3aa0ff;background:rgba(80,160,255,.08);border-radius:4px;color:inherit;";
    bubble.appendChild(box);
  }
  box.textContent = text;
}

// 媒体气泡：OCR/转写原文 + 译文双行渲染
function renderMediaTranslation(bubble, f) {
  const orig = f.original ? "[" + f.label + "原文] " + f.original + "\n" : "";
  renderTranslation(bubble, orig + (f.translated || ""));
}

function bubbleMedia(bubble) {
  try {
    return PROFILE.media ? PROFILE.media(bubble) : null;
  } catch (e) {
    return null;
  }
}

// 取媒体字节 → 纯 base64。blob: 同源可直接 fetch；跨域 http 媒体可能被 CORS 拦（catch 后提示）。
async function mediaToBase64(url) {
  const resp = await fetch(url);
  const blob = await resp.blob();
  return await new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(String(fr.result || "").split(",").pop() || "");
    fr.onerror = reject;
    fr.readAsDataURL(blob);
  });
}

async function translateMediaBubble(bubble, btn, m) {
  const old = btn.textContent;
  btn.textContent = m.kind === "image" ? "识别中…" : "转写中…";
  try {
    const b64 = await mediaToBase64(m.url);
    if (!b64) { btn.textContent = "读取媒体失败"; return; }
    const res = await ipcRenderer.invoke("desktop:translate-media", {
      kind: m.kind, b64, target_lang: targetLang(),
    });
    const f = formatMediaResult(m.kind, res);
    if (!f.ok) { btn.textContent = f.note || "翻译失败"; return; }
    btn.textContent = old;
    renderMediaTranslation(bubble, f);
  } catch (e) {
    btn.textContent = "读取媒体失败";
  }
}

// P0 护栏：归一后与原文一致（identity 兜底/同语种）时不渲染译文，与后台同规则
function _aitrNorm(s) { return String(s == null ? "" : s).replace(/\s+/g, "").toLowerCase(); }
function aitrMeaningful(orig, xl) { return !!xl && _aitrNorm(orig) !== _aitrNorm(xl); }

async function translateBubble(bubble, btn) {
  const text = bubbleVisibleText(bubble);
  if (!text) return;
  const old = btn.textContent;
  btn.textContent = "翻译中…";
  const res = await ipcRenderer.invoke("desktop:translate", { text, target_lang: targetLang() });
  if (res && res.ok && res.text) {
    if (aitrMeaningful(text, res.text)) { btn.textContent = old; renderTranslation(bubble, res.text); }
    else { btn.textContent = "≈ 原文"; } // 同语种/无需翻译：不重复显示原文
  } else { btn.textContent = "翻译失败"; }
}

function appendInjectControl(bubble, el) {
  const anchor =
    bubble.querySelector(".copyable-text, .copyable-area") ||
    bubble.querySelector("[data-testid='msg-container']") ||
    bubble;
  anchor.appendChild(el);
}

function decorateBubble(bubble) {
  if (bubble.getAttribute(PROCESSED)) return;
  const text = bubbleVisibleText(bubble);
  if (text) {
    bubble.setAttribute(PROCESSED, "1");
    const btn = makeBtn("点击翻译");
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      translateBubble(bubble, btn);
    });
    appendInjectControl(bubble, btn);
    if (CONFIG.translate && CONFIG.translate.auto) translateBubble(bubble, btn);
    return;
  }
  // 纯媒体气泡：图片 OCR / 语音转写 → 翻译（按需点击，不随 auto 自动跑，省算力）
  if (!formatMediaResult) return; // 格式化模块缺失 → 媒体翻译降级关闭
  const m = bubbleMedia(bubble);
  if (!m) return; // 系统/其它媒体：不加按钮
  bubble.setAttribute(PROCESSED, "1");
  const btn = makeBtn(m.kind === "image" ? "🖼 翻译图片" : "🎤 翻译语音");
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    translateMediaBubble(bubble, btn, m);
  });
  appendInjectControl(bubble, btn);
}

// ── P1 同步桥：把官方 web 看到的消息回流统一收件箱 ───────────────────────────
function currentPeerName() {
  try {
    return PROFILE.peerName ? PROFILE.peerName() : "";
  } catch (e) {
    return "";
  }
}

function bubbleMid(bubble) {
  try {
    return PROFILE.mid ? PROFILE.mid(bubble) : "";
  } catch (e) {
    return "";
  }
}

function bubblePeerId(bubble) {
  try {
    return PROFILE.peerId ? PROFILE.peerId(bubble) : "";
  } catch (e) {
    return "";
  }
}

let _diagAt = 0;
function ingestDiag(bubbles) {
  const now = Date.now();
  if (now - _diagAt < 5000) return; // 5s 节流
  _diagAt = now;
  let withMid = 0;
  let withPeer = 0;
  bubbles.forEach((b) => {
    if (bubbleMid(b)) withMid++;
    if (bubblePeerId(b)) withPeer++;
  });
  ipcRenderer.invoke(
    "desktop:diag",
    `[sync:${PLATFORM}] bubbles=${bubbles.length} mid=${withMid} peer=${withPeer} hash=${(
      location.hash || ""
    ).slice(0, 24)} pushed=${PUSHED.size}`
  );
}

function ingestBubble(bubble) {
  if (!(CONFIG.sync && CONFIG.sync.enabled)) return;
  if (!PROFILE.canIngest) return;
  const mid = bubbleMid(bubble);
  const peerId = bubblePeerId(bubble);
  if (!mid || !peerId) return;
  const key = peerId + ":" + mid;
  if (PUSHED.has(key)) return;
  const text = bubbleVisibleText(bubble);
  if (!text) return; // P1 先只同步文本，媒体留后续
  PUSHED.add(key);
  ipcRenderer.invoke("desktop:ingest", {
    platform: PLATFORM,
    account_id: ACCOUNT_ID,
    chat_key: String(peerId),
    name: currentPeerName(),
    text,
    direction: PROFILE.isOut(bubble) ? "out" : "in",
    msg_id: String(mid),
    ts: Math.floor(Date.now() / 1000),
  });
}

function isContentBubble(b) {
  try {
    return PROFILE.isContent ? PROFILE.isContent(b) : !!b;
  } catch (e) {
    return false;
  }
}

function scanAll() {
  let bubbles = Array.from(document.querySelectorAll(PROFILE.bubble)).filter(isContentBubble);
  // WhatsApp：优先 message-in/out 根节点，避免 data-id 子节点与父节点重复装饰
  if (PLATFORM === "whatsapp") {
    const roots = bubbles.filter(
      (b) => b.classList && (b.classList.contains("message-in") || b.classList.contains("message-out"))
    );
    if (roots.length) bubbles = roots;
  }
  bubbles.forEach((b) => {
    decorateBubble(b);
    ingestBubble(b);
  });
  if (CONFIG.sync && CONFIG.sync.enabled && bubbles.length) ingestDiag(bubbles);
  maybeReportActiveChat(bubbles);
  reportInjectStatus(bubbles);
}

// ── 注入健康状态上报：让壳层 renderer 在平台 Tab 顶部显示「注入正常/失配/未登录」──
// 让坐席一眼区分是「注入坏了（官方改版选择器失配）」还是「没登录/没开会话」。
function findComposer() {
  try { return PROFILE.composer ? document.querySelector(PROFILE.composer) : null; } catch (e) { return null; }
}
let _statusAt = 0;
let _lastStatusSig = "";
function reportInjectStatus(bubbles) {
  const now = Date.now();
  if (now - _statusAt < 2500) return; // 2.5s 节流
  _statusAt = now;
  let count = 0;
  try {
    count = (bubbles || Array.from(document.querySelectorAll(PROFILE.bubble)).filter(isContentBubble)).length;
  } catch (e) { count = 0; }
  const composer = !!findComposer();
  const peer = currentPeerName();
  const chatOpen = !!peer || count > 0;
  // 状态指纹：仅在变化时上报，避免刷屏
  const sig = (composer ? "1" : "0") + "|" + (count > 0 ? "1" : "0") + "|" + (chatOpen ? "1" : "0");
  if (sig === _lastStatusSig) return;
  _lastStatusSig = sig;
  try {
    ipcRenderer.sendToHost("inject-status", {
      platform: PLATFORM,
      account_id: ACCOUNT_ID,
      supported: !!PROFILE.supported,
      composer,
      bubbles: count,
      chatOpen,
    });
  } catch (e) {
    /* 非 webview 宿主环境，忽略 */
  }
}

// ── P2 业务右栏：向壳层 renderer 上报当前会话（profile/KB/智能草拟用）─────────
let _lastChatPeer = "";
let _lastTopMid = "";
let _lastReportAt = 0;
function maybeReportActiveChat(bubbles) {
  let peer = "";
  let topMid = "";
  for (let i = bubbles.length - 1; i >= 0; i--) {
    if (!peer) peer = bubblePeerId(bubbles[i]);
    if (!topMid) topMid = bubbleMid(bubbles[i]);
    if (peer && topMid) break;
  }
  if (!peer && PROFILE.conversationPeerId) {
    try { peer = PROFILE.conversationPeerId(); } catch (e) { /* ignore */ }
  }
  if (!peer) return;
  const now = Date.now();
  const switched = peer !== _lastChatPeer;
  const grew = topMid && topMid !== _lastTopMid && now - _lastReportAt > 2000;
  if (!switched && !grew) return;
  _lastChatPeer = peer;
  _lastTopMid = topMid;
  _lastReportAt = now;
  try {
    ipcRenderer.sendToHost("active-chat", {
      platform: PLATFORM,
      account_id: ACCOUNT_ID,
      chat_key: String(peer),
      name: currentPeerName(),
      switched,
      messages: collectRecentMessages(12),
    });
    ipcRenderer.invoke("desktop:diag", `[panel:${PLATFORM}] active-chat → ${peer} (${currentPeerName()})`);
  } catch (e) {
    /* 非 webview 宿主环境，忽略 */
  }
}

// ── 智能回复浮钮 ───────────────────────────────────────────────────────────
function collectRecentMessages(limit) {
  const out = [];
  const bubbles = Array.from(document.querySelectorAll(PROFILE.bubble))
    .filter(isContentBubble)
    .slice(-(limit || 12));
  for (const b of bubbles) {
    const text = bubbleVisibleText(b);
    if (!text) continue;
    out.push({ direction: PROFILE.isOut(b) ? "out" : "in", text });
  }
  return out;
}

function fillComposer(text) {
  const el = document.querySelector(PROFILE.composer);
  if (!el) return false;
  el.focus();
  if (PROFILE.richInput) {
    // React/Lexical 富文本编辑器（WhatsApp/Messenger）：textContent 不会被识别，
    // 用 execCommand insertText 走浏览器原生输入路径，最稳。
    try {
      document.execCommand("selectAll", false, null);
      if (document.execCommand("insertText", false, text)) {
        el.dispatchEvent(new InputEvent("input", { bubbles: true }));
        return true;
      }
    } catch (e) {
      /* 回落到 textContent */
    }
  }
  el.textContent = text;
  el.dispatchEvent(new InputEvent("input", { bubbles: true }));
  return true;
}

function sendComposer() {
  // 优先点官方发送按钮；取不到则回落在输入框上派发回车
  const btn = document.querySelector(PROFILE.sendBtn);
  if (btn) {
    (btn.closest("button") || btn).click();
    return true;
  }
  const el = document.querySelector(PROFILE.composer);
  if (!el) return false;
  el.focus();
  ["keydown", "keypress", "keyup"].forEach((type) =>
    el.dispatchEvent(
      new KeyboardEvent(type, {
        key: "Enter",
        code: "Enter",
        keyCode: 13,
        which: 13,
        bubbles: true,
        cancelable: true,
      })
    )
  );
  return true;
}

function mountSmartReplyButton() {
  if (document.getElementById("aitr-smart")) return;
  const fab = document.createElement("div");
  fab.id = "aitr-smart";
  fab.textContent = "🤖 智能回复";
  fab.style.cssText =
    "position:fixed;right:18px;bottom:90px;z-index:99999;padding:8px 14px;border-radius:20px;" +
    "background:#3aa0ff;color:#fff;font-size:13px;cursor:pointer;box-shadow:0 2px 10px rgba(0,0,0,.3);";
  fab.addEventListener("click", async () => {
    const old = fab.textContent;
    fab.textContent = "生成中…";
    const messages = collectRecentMessages(12);
    const res = await ipcRenderer.invoke("desktop:smart-reply", {
      messages,
      platform: PLATFORM,
      persona_id: CURRENT_PERSONA,
      target_lang: REPLY_LANG,
    });
    fab.textContent = old;
    if (res && res.ok && res.reply) fillComposer(res.translated || res.reply);
  });
  document.body.appendChild(fab);
}

async function selfTest() {
  // 自检：不依赖登录，验证「注入→IPC→主进程→后端翻译」整条链路
  try {
    ipcRenderer.invoke("desktop:diag", `inject loaded (${PLATFORM}); running self-test…`);
    const res = await ipcRenderer.invoke("desktop:translate", {
      text: "Hello, how can we cooperate?",
      target_lang: "zh",
    });
    ipcRenderer.invoke("desktop:diag", "self-test translate => " + JSON.stringify(res));
  } catch (e) {
    ipcRenderer.invoke("desktop:diag", "self-test ERROR " + String(e));
  }
}

async function start() {
  try {
    const c = await ipcRenderer.invoke("desktop:config");
    if (c) CONFIG = c;
  } catch (e) {
    /* 用默认 CONFIG */
  }

  // 壳层右栏切换人设 → 同步给浮钮「智能回复」，保持两处一致（所有平台都监听）
  ipcRenderer.on("set-persona", (_e, payload) => {
    CURRENT_PERSONA = (payload && payload.persona_id) || "";
  });
  // 壳层右栏切换「回复语言」→ 浮钮智能回复目标语言同步
  ipcRenderer.on("set-reply-lang", (_e, payload) => {
    REPLY_LANG = (payload && payload.target_lang) || "";
  });
  // 壳层 renderer 下发本 webview 的归属账号 → 同步/智能回复都带上正确 account_id
  ipcRenderer.on("set-account", (_e, payload) => {
    ACCOUNT_ID = (payload && payload.account_id) || "";
  });

  if (!PROFILE.supported) {
    // 不支持的平台（messenger/line/unknown）：不挂任何 UI，绝不把消息误标成 telegram。
    ipcRenderer.invoke(
      "desktop:diag",
      `[inject] 平台「${PLATFORM}」暂无选择器档案，已跳过注入（仅「填入」依赖的右栏功能不可用）。`
    );
    return;
  }

  if (CONFIG.debug) selfTest();

  // 壳层右栏点「填入 / 填入并发送」→ 写进官方 web 输入框，可选自动发送
  ipcRenderer.on("fill-composer", (_e, payload) => {
    const text = typeof payload === "string" ? payload : (payload && payload.text) || "";
    const send = typeof payload === "object" && payload && payload.send;
    if (!text) return;
    fillComposer(String(text));
    if (send) setTimeout(sendComposer, 150); // 等 input 事件落定再发送
  });

  const obs = new MutationObserver(() => scanAll());
  // 关键：除子节点外，也监听 data-mid/data-id 属性变化——webk/WhatsApp 都是「先插气泡、
  // 稍后补 id」，只监听 childList 会漏掉出站消息（导致发出的回复同步不到收件箱）。
  obs.observe(document.documentElement, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ["data-mid", "data-peer-id", "data-id"],
  });
  scanAll();
  setInterval(scanAll, 2000); // 兜底周期扫描，确保迟到的 id / 漏掉的变更最终被捕获
  mountSmartReplyButton();
  setInterval(mountSmartReplyButton, 3000); // 切换会话后浮钮可能被重建，兜底重挂
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", start);
} else {
  start();
}
