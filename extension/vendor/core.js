"use strict";

/* 多平台内容注入核心（单一事实来源）——平台无关业务逻辑：
 *   点击翻译 / 媒体翻译 / 智能回复浮钮 / 同步桥回流 / 注入健康上报 / 当前会话上报。
 *
 * 历史上这段逻辑内联在 desktop/inject/tg-inject.js 并直接耦合 Electron 的 ipcRenderer。
 * 现抽成 createInject(host, deps)：把所有「与宿主对话」的动作收口到 host 适配层，
 * 让桌面（Electron preload，host=ipcRenderer 实现）与浏览器扩展（content script，
 * host=chrome.runtime 实现）复用同一份核心，零重复。
 *
 * host 契约（全部可返回 Promise；不需要的能力可省略，core 会安全降级）：
 *   translate({text,target_lang})            -> {ok,text}
 *   translateMedia({kind,b64,target_lang})   -> 后端媒体翻译原始响应
 *   smartReply({messages,platform,persona_id,target_lang}) -> {ok,reply,translated}
 *   ingest(payload)                          -> 回流一条消息到统一收件箱
 *   getConfig()                              -> 运行配置 {translate,sync,debug,...}
 *   getSelectorProfiles()                    -> {ok,profiles} 远程选择器覆写
 *   diag(msg)                                -> 诊断日志
 *   injectHealth(payload)                    -> 注入健康信标（后端看板）
 *   reportInjectStatus(payload)              -> 注入状态上报给宿主 UI（桌面 sendToHost）
 *   reportActiveChat(payload)                -> 当前会话上报给宿主右栏
 *   onSetPersona(cb)/onSetReplyLang(cb)/onSetAccount(cb)/onFillComposer(cb) -> 宿主下行事件
 *
 * deps：{ profiles, mediaFormat }（即 shared/inject/profiles.js、media-format.js 的导出）。
 */

function createInject(host, deps) {
  host = host || {};
  deps = deps || {};
  const profiles = deps.profiles || {};
  const mediaFormat = deps.mediaFormat || null;
  const formatMediaResult = mediaFormat && mediaFormat.formatMediaResult;

  const detectPlatform = profiles.detectPlatform || function () { return "unknown"; };
  const BUILTIN_PROFILES = profiles.BUILTIN_PROFILES || {};
  const applySelectorOverlay = profiles.applySelectorOverlay || function (p) { return p; };
  const selectorHealth = profiles.selectorHealth || null;

  // host 方法安全包装：缺失能力 → no-op / 安全默认，绝不抛错中断扫描循环。
  function call(name, args, fallback) {
    const fn = host[name];
    if (typeof fn !== "function") return Promise.resolve(fallback);
    try {
      return Promise.resolve(fn(args));
    } catch (e) {
      return Promise.resolve(fallback);
    }
  }
  function fire(name, payload) {
    const fn = host[name];
    if (typeof fn !== "function") return;
    try { fn(payload); } catch (e) { /* 宿主缺失/异常：忽略 */ }
  }
  function on(name, cb) {
    const fn = host[name];
    if (typeof fn === "function") {
      try { fn(cb); } catch (e) { /* ignore */ }
    }
  }
  function diag(msg) { fire("diag", msg); }

  const PLATFORM = detectPlatform();
  let PROFILE = BUILTIN_PROFILES[PLATFORM] || { platform: PLATFORM, supported: false };

  // 远程选择器覆写：非阻塞，成功则就地热更新 PROFILE，失败静默用内置档。
  (async function bootstrapOverlay() {
    if (!BUILTIN_PROFILES[PLATFORM]) return;
    try {
      const res = await call("getSelectorProfiles", undefined, null);
      const remote = res && res.ok && res.profiles;
      if (remote && remote[PLATFORM]) {
        PROFILE = applySelectorOverlay(BUILTIN_PROFILES[PLATFORM], remote[PLATFORM]);
        diag(`[selector-overlay:${PLATFORM}] applied`);
      }
    } catch (e) { /* 静默用内置档 */ }
  })();

  const PUSHED = new Set();
  let CONFIG = { translate: { target_lang: "zh", auto: false } };
  let CURRENT_PERSONA = "";
  let REPLY_LANG = "";
  let ACCOUNT_ID = "";

  const PROCESSED = "data-aitr";

  function targetLang() {
    return (CONFIG.translate && CONFIG.translate.target_lang) || "zh";
  }

  function bubbleVisibleText(bubble) {
    try { return PROFILE.text ? PROFILE.text(bubble) : ""; } catch (e) { return ""; }
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

  function renderMediaTranslation(bubble, f) {
    const orig = f.original ? "[" + f.label + "原文] " + f.original + "\n" : "";
    renderTranslation(bubble, orig + (f.translated || ""));
  }

  function bubbleMedia(bubble) {
    try { return PROFILE.media ? PROFILE.media(bubble) : null; } catch (e) { return null; }
  }

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
      const res = await call("translateMedia", { kind: m.kind, b64, target_lang: targetLang() }, null);
      const f = formatMediaResult(m.kind, res);
      if (!f.ok) { btn.textContent = f.note || "翻译失败"; return; }
      btn.textContent = old;
      renderMediaTranslation(bubble, f);
    } catch (e) {
      btn.textContent = "读取媒体失败";
    }
  }

  function _aitrNorm(s) { return String(s == null ? "" : s).replace(/\s+/g, "").toLowerCase(); }
  function aitrMeaningful(orig, xl) { return !!xl && _aitrNorm(orig) !== _aitrNorm(xl); }

  async function translateBubble(bubble, btn) {
    const text = bubbleVisibleText(bubble);
    if (!text) return;
    const old = btn.textContent;
    btn.textContent = "翻译中…";
    const res = await call("translate", { text, target_lang: targetLang() }, null);
    if (res && res.ok && res.text) {
      if (aitrMeaningful(text, res.text)) { btn.textContent = old; renderTranslation(bubble, res.text); }
      else { btn.textContent = "≈ 原文"; }
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
      btn.addEventListener("click", (e) => { e.stopPropagation(); translateBubble(bubble, btn); });
      appendInjectControl(bubble, btn);
      if (CONFIG.translate && CONFIG.translate.auto) translateBubble(bubble, btn);
      return;
    }
    if (!formatMediaResult) return;
    const m = bubbleMedia(bubble);
    if (!m) return;
    bubble.setAttribute(PROCESSED, "1");
    const btn = makeBtn(m.kind === "image" ? "🖼 翻译图片" : "🎤 翻译语音");
    btn.addEventListener("click", (e) => { e.stopPropagation(); translateMediaBubble(bubble, btn, m); });
    appendInjectControl(bubble, btn);
  }

  function currentPeerName() {
    try { return PROFILE.peerName ? PROFILE.peerName() : ""; } catch (e) { return ""; }
  }
  function bubbleMid(bubble) {
    try { return PROFILE.mid ? PROFILE.mid(bubble) : ""; } catch (e) { return ""; }
  }
  function bubblePeerId(bubble) {
    try { return PROFILE.peerId ? PROFILE.peerId(bubble) : ""; } catch (e) { return ""; }
  }

  let _diagAt = 0;
  function ingestDiag(bubbles) {
    const now = Date.now();
    if (now - _diagAt < 5000) return;
    _diagAt = now;
    let withMid = 0;
    let withPeer = 0;
    bubbles.forEach((b) => {
      if (bubbleMid(b)) withMid++;
      if (bubblePeerId(b)) withPeer++;
    });
    diag(
      `[sync:${PLATFORM}] bubbles=${bubbles.length} mid=${withMid} peer=${withPeer} hash=${(
        (typeof location !== "undefined" && location.hash) || ""
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
    if (!text) return;
    PUSHED.add(key);
    fire("ingest", {
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
    try { return PROFILE.isContent ? PROFILE.isContent(b) : !!b; } catch (e) { return false; }
  }

  function scanAll() {
    let bubbles = Array.from(document.querySelectorAll(PROFILE.bubble)).filter(isContentBubble);
    if (PLATFORM === "whatsapp") {
      const roots = bubbles.filter(
        (b) => b.classList && (b.classList.contains("message-in") || b.classList.contains("message-out"))
      );
      if (roots.length) bubbles = roots;
    }
    bubbles.forEach((b) => { decorateBubble(b); ingestBubble(b); });
    if (CONFIG.sync && CONFIG.sync.enabled && bubbles.length) ingestDiag(bubbles);
    maybeReportActiveChat(bubbles);
    reportInjectStatus(bubbles);
  }

  function findComposer() {
    try { return PROFILE.composer ? document.querySelector(PROFILE.composer) : null; } catch (e) { return null; }
  }
  let _statusAt = 0;
  let _lastStatusSig = "";
  let _healthBeaconAt = 0;
  const _HEALTH_HEARTBEAT_MS = 30000;
  function reportInjectStatus(bubbles) {
    const now = Date.now();
    if (now - _statusAt < 2500) return;
    _statusAt = now;
    let count = 0;
    try {
      count = (bubbles || Array.from(document.querySelectorAll(PROFILE.bubble)).filter(isContentBubble)).length;
    } catch (e) { count = 0; }
    const composer = !!findComposer();
    const peer = currentPeerName();
    const chatOpen = !!peer || count > 0;
    let selectors = { bubble: count > 0, composer, sendBtn: false, peerTitle: !!peer };
    try {
      if (selectorHealth) selectors = selectorHealth(PROFILE);
    } catch (e) { /* 探针异常：用粗粒度兜底 */ }
    const payload = {
      platform: PLATFORM,
      account_id: ACCOUNT_ID,
      supported: !!PROFILE.supported,
      generic: !!PROFILE.generic,
      can_ingest: !!PROFILE.canIngest,
      composer,
      bubbles: count,
      chatOpen,
      selectors,
    };
    const sig = (composer ? "1" : "0") + "|" + (count > 0 ? "1" : "0") + "|" + (chatOpen ? "1" : "0");
    const changed = sig !== _lastStatusSig;
    if (changed) {
      _lastStatusSig = sig;
      fire("reportInjectStatus", payload);
    }
    if (changed || now - _healthBeaconAt >= _HEALTH_HEARTBEAT_MS) {
      _healthBeaconAt = now;
      fire("injectHealth", payload);
    }
  }

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
    fire("reportActiveChat", {
      platform: PLATFORM,
      account_id: ACCOUNT_ID,
      chat_key: String(peer),
      name: currentPeerName(),
      switched,
      messages: collectRecentMessages(12),
    });
    diag(`[panel:${PLATFORM}] active-chat → ${peer} (${currentPeerName()})`);
  }

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
      try {
        document.execCommand("selectAll", false, null);
        if (document.execCommand("insertText", false, text)) {
          el.dispatchEvent(new InputEvent("input", { bubbles: true }));
          return true;
        }
      } catch (e) { /* 回落到 textContent */ }
    }
    el.textContent = text;
    el.dispatchEvent(new InputEvent("input", { bubbles: true }));
    return true;
  }

  function sendComposer() {
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
          key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true,
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
      const res = await call("smartReply", {
        messages, platform: PLATFORM, persona_id: CURRENT_PERSONA, target_lang: REPLY_LANG,
      }, null);
      fab.textContent = old;
      if (res && res.ok && res.reply) fillComposer(res.translated || res.reply);
    });
    document.body.appendChild(fab);
  }

  async function selfTest() {
    try {
      diag(`inject loaded (${PLATFORM}); running self-test…`);
      const res = await call("translate", { text: "Hello, how can we cooperate?", target_lang: "zh" }, null);
      diag("self-test translate => " + JSON.stringify(res));
    } catch (e) {
      diag("self-test ERROR " + String(e));
    }
  }

  async function start() {
    try {
      const c = await call("getConfig", undefined, null);
      if (c) CONFIG = c;
    } catch (e) { /* 用默认 CONFIG */ }

    on("onSetPersona", (payload) => { CURRENT_PERSONA = (payload && payload.persona_id) || ""; });
    on("onSetReplyLang", (payload) => { REPLY_LANG = (payload && payload.target_lang) || ""; });
    on("onSetAccount", (payload) => { ACCOUNT_ID = (payload && payload.account_id) || ""; });

    if (!PROFILE.supported) {
      diag(`[inject] 平台「${PLATFORM}」暂无选择器档案，已跳过注入。`);
      return;
    }

    if (CONFIG.debug) selfTest();

    on("onFillComposer", (payload) => {
      const text = typeof payload === "string" ? payload : (payload && payload.text) || "";
      const send = typeof payload === "object" && payload && payload.send;
      if (!text) return;
      fillComposer(String(text));
      if (send) setTimeout(sendComposer, 150);
    });

    const obs = new MutationObserver(() => scanAll());
    obs.observe(document.documentElement, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["data-mid", "data-peer-id", "data-id"],
    });
    scanAll();
    setInterval(scanAll, 2000);
    mountSmartReplyButton();
    setInterval(mountSmartReplyButton, 3000);
  }

  function autostart() {
    if (typeof document === "undefined") return;
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", start);
    } else {
      start();
    }
  }

  return { start, autostart, get platform() { return PLATFORM; } };
}

// 双模式导出（单一源，桌面 preload 与浏览器扩展 content script 共用）。
(function (api) {
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (typeof globalThis !== "undefined") {
    globalThis.AInjectCore = api;
  }
})({ createInject });
