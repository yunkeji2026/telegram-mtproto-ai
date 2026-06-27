"use strict";

// 选择器档案（PROFILES）——从 tg-inject.js 外移，便于：①热更新（后端覆写层）②单测 ③扩展新平台。
// 双模式：webview preload(tg-inject.js) require 本文件；Node 单测亦 require（纯函数，无 DOM/electron 依赖）。
//
// 三类档案：
//   - 内置定制档（telegram / whatsapp）：保留原逐字实现（自定义 text/mid/peerId 解析），零回归。
//   - 通用工厂档（instagram / messenger / x / zalo）：由声明式选择器经 makeGenericProfile() 生成，
//     新增平台只需填选择器数据 → 天然可热更新。默认 canIngest=false（选择器现场 F12 校准前
//     宁可不回流，避免污染统一收件箱——「宁缺毋错」），翻译/智能回复/注入状态不受影响。
//   - 覆写层（applySelectorOverlay）：后端 /api/desktop/selector-profiles 下发的选择器修正，
//     官方改版后无需重发桌面包即可热修（仅覆盖白名单字段）。

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
// hostname 可显式传入（单测）；缺省读 location.hostname（preload 运行时）。
function detectPlatform(hostname) {
  const h = String(
    hostname != null ? hostname : (typeof location !== "undefined" ? location.hostname : "")
  ).toLowerCase();
  if (h.indexOf("web.telegram.org") >= 0) return "telegram";
  if (h.indexOf("web.whatsapp.com") >= 0) return "whatsapp";
  if (h.indexOf("messenger.com") >= 0) return "messenger";
  if (h.indexOf("instagram.com") >= 0) return "instagram";
  if (h.indexOf("x.com") >= 0 || h.indexOf("twitter.com") >= 0) return "x";
  if (h.indexOf("zalo.me") >= 0) return "zalo";
  // facebook.com 历史上回落 messenger（站内私信），保留兼容
  if (h.indexOf("facebook.com") >= 0) return "messenger";
  if (h.indexOf("line.me") >= 0 || h.indexOf("line-apps.com") >= 0) return "line";
  return "unknown";
}

// djb2 字符串散列 → 稳定短 id（通用档无原生 msg_id 时的 mid 兜底，使 PUSHED 去重可用）。
function _hash(s) {
  let h = 5381;
  const str = String(s || "");
  for (let i = 0; i < str.length; i++) h = ((h << 5) + h + str.charCodeAt(i)) | 0;
  return (h >>> 0).toString(36);
}

function _firstAttr(node, attrs) {
  if (!node || !node.getAttribute) return "";
  for (let i = 0; i < attrs.length; i++) {
    const v = node.getAttribute(attrs[i]);
    if (v) return v;
    if (node.closest) {
      const host = node.closest("[" + attrs[i] + "]");
      if (host) {
        const hv = host.getAttribute(attrs[i]);
        if (hv) return hv;
      }
    }
  }
  return "";
}

// ── 通用档工厂 ───────────────────────────────────────────────────────────────
// cfg: { platform, supported?, canIngest?, richInput?, bubble, bubbleText, composer,
//        sendBtn, peerTitle, outFlag?, outSelector?, excludeClasses?, midAttrs?, peerAttrs?,
//        urlPeerRegex?, mediaImg?, mediaAudio?, hashMid? }
function makeGenericProfile(cfg) {
  const c = cfg || {};
  return {
    platform: c.platform || "unknown",
    supported: c.supported !== false,
    // 默认不回流：选择器未现场校准前，避免把方向错标/碎片消息灌进统一收件箱。
    canIngest: c.canIngest === true,
    richInput: c.richInput !== false, // 多数现代 web 端是 contenteditable
    generic: true,
    bubble: c.bubble || "",
    bubbleText: c.bubbleText || "",
    composer: c.composer || "",
    sendBtn: c.sendBtn || "",
    peerTitle: c.peerTitle || "",
    outFlag: c.outFlag || "",
    outSelector: c.outSelector || "",
    mediaImg: c.mediaImg || "",
    mediaAudio: c.mediaAudio || "",
    _excl: c.excludeClasses || [],
    _midAttrs: c.midAttrs || ["data-id", "data-mid", "data-message-id"],
    _peerAttrs: c.peerAttrs || ["data-thread-id", "data-peer-id"],
    _urlPeerRe: c.urlPeerRegex || "",
    _hashMid: c.hashMid !== false, // 无原生 mid 时用文本散列兜底

    isContent(b) {
      if (!b || !b.classList) return !!b;
      return !this._excl.some((x) => b.classList.contains(x));
    },
    isOut(b) {
      if (!b) return false;
      if (this.outFlag && b.classList) return b.classList.contains(this.outFlag);
      if (this.outSelector && b.matches) {
        try { return b.matches(this.outSelector); } catch (e) { return false; }
      }
      return false; // 无法判向：保守标 in（方向仅影响 ingest，默认关闭）
    },
    text(b) {
      if (!b) return "";
      const node = (this.bubbleText && b.querySelector && b.querySelector(this.bubbleText)) || b;
      let clone;
      try { clone = node.cloneNode(true); } catch (e) { clone = node; }
      if (clone.querySelectorAll) {
        clone.querySelectorAll(".aitr-box,.aitr-btn,time,[role='button']").forEach((n) => n.remove());
      }
      return cleanVisibleText(clone.textContent || "");
    },
    mid(b) {
      const native = _firstAttr(b, this._midAttrs);
      if (native) return native;
      if (this._hashMid) {
        const t = this.text(b);
        return t ? _hash(this.peerId(b) + "|" + t) : "";
      }
      return "";
    },
    peerId(b) {
      const native = _firstAttr(b, this._peerAttrs);
      if (native) return native;
      if (this._urlPeerRe && typeof location !== "undefined") {
        try {
          const m = (location.pathname + location.hash).match(new RegExp(this._urlPeerRe));
          if (m && m[1]) return m[1];
        } catch (e) { /* 无效正则：忽略 */ }
      }
      return this.peerName ? this.peerName() : "";
    },
    peerName() {
      if (!this.peerTitle || typeof document === "undefined") return "";
      const el = document.querySelector(this.peerTitle);
      return el ? (el.getAttribute("title") || el.textContent || "").trim() : "";
    },
    media(b) {
      if (!b || !b.querySelector) return null;
      if (this.mediaImg) {
        const img = b.querySelector(this.mediaImg);
        if (img && img.src && img.src.indexOf("data:") !== 0) return { kind: "image", url: img.src };
      }
      if (this.mediaAudio) {
        const a = b.querySelector(this.mediaAudio);
        if (a && a.getAttribute("src")) return { kind: "voice", url: a.src };
      }
      return null;
    },
  };
}

// ── 内置定制档（telegram / whatsapp）：逐字保留原实现，零回归 ──────────────────
const _telegram = {
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
    return !(
      b.classList.contains("service") ||
      b.classList.contains("is-date") ||
      b.classList.contains("is-system") ||
      b.classList.contains("is-sponsored") ||
      b.classList.contains("bubble-first-unread")
    );
  },
  text(bubble) {
    const docName = bubble.querySelector(
      ".document-name, .document .name, .media-container .document-name"
    );
    if (docName && !bubble.querySelector(this.bubbleText)) {
      const name = (docName.textContent || "").replace(/\s+/g, " ").trim();
      return name ? "[文件] " + name : "[文件]";
    }
    const node = bubble.querySelector(this.bubbleText) || bubble;
    const clone = node.cloneNode(true);
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
  media(bubble) {
    const img = bubble.querySelector(".media-photo, .media-container img, .attachment img");
    if (img && img.src && img.src.indexOf("data:") !== 0) return { kind: "image", url: img.src };
    const audio = bubble.querySelector("audio[src]");
    if (audio && audio.getAttribute("src")) return { kind: "voice", url: audio.src };
    return null;
  },
};

const _whatsapp = {
  platform: "whatsapp",
  supported: true,
  canIngest: true,
  richInput: true,
  bubble: "div.message-in, div.message-out, div[data-id*='@c.us'], div[data-id*='@g.us']",
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
    const sel =
      '#main [data-id*="@c.us"], #main [data-id*="@g.us"], ' +
      'header [data-id*="@c.us"], header [data-id*="@g.us"]';
    const el = document.querySelector(sel);
    if (!el) return "";
    const parts = (el.getAttribute("data-id") || "").split("_");
    if (parts.length >= 2 && parts[1].indexOf("@") >= 0) return parts[1];
    return "";
  },
  media(bubble) {
    const img = bubble.querySelector('img[src^="blob:"], img[src^="http"]');
    if (img && img.src) return { kind: "image", url: img.src };
    const audio = bubble.querySelector("audio[src]");
    if (audio && audio.getAttribute("src")) return { kind: "voice", url: audio.src };
    return null;
  },
};

// ── Tier1 通用档（声明式；选择器需现场 F12 校准，默认 canIngest=false）──────────
// 校准/改版后可经 /api/desktop/selector-profiles 覆写层热更新，无需重发桌面包。
const _instagram = makeGenericProfile({
  platform: "instagram",
  richInput: true,
  bubble: "div[role='row']",
  bubbleText: "div[dir='auto']",
  composer: "div[role='textbox'][contenteditable='true'], textarea[placeholder]",
  sendBtn: "div[role='button'][aria-label='Send'], button[type='submit']",
  peerTitle: "header div[role='heading'], header h1, header span[dir='auto']",
  excludeClasses: [],
  urlPeerRegex: "/direct/t/([^/]+)",
  mediaImg: "div[role='row'] img[src^='http']",
});

const _messenger = makeGenericProfile({
  platform: "messenger",
  richInput: true,
  bubble: "div[role='row']",
  bubbleText: "div[dir='auto']",
  composer: "div[role='textbox'][contenteditable='true']",
  sendBtn: "div[role='button'][aria-label='Press enter to send'], div[aria-label='Send']",
  peerTitle: "div[role='main'] h1 span, header span[dir='auto']",
  urlPeerRegex: "/t/([^/]+)",
  mediaImg: "div[role='row'] img[src^='http']",
});

const _x = makeGenericProfile({
  platform: "x",
  richInput: true,
  bubble: "div[data-testid='messageEntry']",
  bubbleText: "div[dir='auto'] span, span",
  composer: "div[data-testid='dmComposerTextInput'], div[role='textbox'][contenteditable='true']",
  sendBtn: "div[data-testid='dmComposerSendButton']",
  peerTitle: "div[data-testid='DM_Conversation_Header'] span, header h2 span",
  urlPeerRegex: "/messages/([^/]+)",
  mediaImg: "div[data-testid='messageEntry'] img[src^='http']",
});

const _zalo = makeGenericProfile({
  platform: "zalo",
  richInput: true,
  bubble: ".chat-item, div[id^='div_message']",
  bubbleText: ".card-text, .text-msg, span",
  composer: "#input_chatInput, div[contenteditable='true'][id*='input']",
  sendBtn: ".btn-send, .send-button, [class*='btn_send']",
  peerTitle: ".conv-title, .header-title, .truncate",
  mediaImg: ".chat-item img[src^='http'], .chat-item img[src^='blob:']",
});

const BUILTIN_PROFILES = {
  telegram: _telegram,
  whatsapp: _whatsapp,
  instagram: _instagram,
  messenger: _messenger,
  x: _x,
  zalo: _zalo,
};

// ── 覆写层（后端热更新）──────────────────────────────────────────────────────
// 仅这些字段可被远程覆盖（选择器字符串 + 少量布尔开关）；自定义解析函数永不可被远程替换（安全）。
const OVERLAYABLE_KEYS = [
  "bubble", "bubbleText", "composer", "sendBtn", "peerTitle",
  "outFlag", "outSelector", "mediaImg", "mediaAudio",
  "supported", "canIngest", "richInput",
];

// 把后端档案 patch 浅合并到内置档（不改原对象，返回浅拷贝）。patch 缺失/非法字段忽略。
function applySelectorOverlay(profile, patch) {
  if (!profile) return profile;
  const out = Object.assign(Object.create(Object.getPrototypeOf(profile)), profile);
  if (!patch || typeof patch !== "object") return out;
  OVERLAYABLE_KEYS.forEach((k) => {
    if (patch[k] === undefined || patch[k] === null) return;
    const cur = out[k];
    // 类型守卫：布尔字段只接受布尔，字符串字段只接受非空字符串
    if (typeof cur === "boolean") {
      if (typeof patch[k] === "boolean") out[k] = patch[k];
    } else if (typeof patch[k] === "string" && patch[k].length > 0) {
      out[k] = patch[k];
    }
  });
  return out;
}

// 解析平台档：取内置档 → 应用覆写层（若有）→ 兜底 unsupported。
function resolveProfile(platform, overlay) {
  const base = BUILTIN_PROFILES[platform];
  if (!base) return { platform: platform, supported: false };
  const patch = overlay && overlay[platform];
  return patch ? applySelectorOverlay(base, patch) : base;
}

// ── D1b 选择器健康探针 ───────────────────────────────────────────────────────
// 逐个关键选择器探测 DOM 是否命中 → {bubble, composer, sendBtn, peerTitle} 布尔表。
// 供注入健康信标上报：官方改版导致某选择器失配时，运营能精确看到「IG composer 失配」而非笼统「坏了」。
// doc 可显式传入（单测）；缺省读全局 document（preload 运行时）。容错：任何异常视为未命中。
function selectorHealth(profile, doc) {
  const d = doc || (typeof document !== "undefined" ? document : null);
  function probe(sel) {
    if (!sel || !d || !d.querySelector) return false;
    try { return !!d.querySelector(sel); } catch (e) { return false; }
  }
  const p = profile || {};
  return {
    bubble: probe(p.bubble),
    composer: probe(p.composer),
    sendBtn: probe(p.sendBtn),
    peerTitle: probe(p.peerTitle),
  };
}

// 双模式导出（单一源，桌面 preload 与浏览器扩展 content script 共用）：
//   - Node/Electron preload：CommonJS require → module.exports。
//   - 浏览器扩展 content script（隔离世界无 require）：挂到 globalThis.AInjectProfiles。
(function (api) {
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (typeof globalThis !== "undefined") {
    globalThis.AInjectProfiles = api;
  }
})({
  cleanVisibleText,
  detectPlatform,
  makeGenericProfile,
  applySelectorOverlay,
  resolveProfile,
  selectorHealth,
  BUILTIN_PROFILES,
  OVERLAYABLE_KEYS,
});
