"use strict";

const ICONS = { telegram: "✈️", whatsapp: "🟢", line: "💬", messenger: "💠", instagram: "📷", signal: "🔵" };

// 统一收件箱标签页的固定 id（区别于 config.accounts[] 的真实账号）
const INBOX_ID = "__inbox__";

// 可内嵌官方网页并注入的平台（须与 inject/tg-inject.js 的 PROFILES[*].supported 保持一致）。
// Messenger/LINE 无可用网页版聊天 → 不开内嵌死页，统一引导到收件箱。
const EMBEDDABLE = { telegram: true, whatsapp: true };
function isEmbeddable(platform) { return !!EMBEDDABLE[platform]; }

// 注入诊断：保存各账号最近一次 inject-status 上报，按激活 Tab 渲染顶部状态条。
// deriveInjectState 由 inject-status.js 提供（浏览器全局；亦可 node 单测）。
const InjectStatus = { byId: {}, el: null, activeId: null };

// 统一收件箱运行态（供 rail 切换时联动遮罩显隐）：wv=后台 webview，overlay=连接/错误遮罩，
// applyVisibility(active)=切到本标签时按当前阶段决定遮罩显隐。
const Inbox = { wv: null, overlay: null, phase: "init", applyVisibility: null };

// 后台 /workspace 走 session cookie 鉴权；webview 落到 /login 时在页面内 POST 凭据自动登录后回跳。
// cred 为 {auth_token} 或 {username,password}；成功/失败都 location.replace 回目标页——
// 失败会被后端再 303 回 /login，由调用方据「再次到 /login」切到下一组凭据或转人工。
function backendLoginJS(cred, path) {
  const repl = "location.replace(" + JSON.stringify(path) + ");";
  return (
    "(function(){try{" +
    "var c=" + JSON.stringify(cred || {}) + ";" +
    "var b=Object.keys(c).map(function(k){return k+'='+encodeURIComponent(c[k]);}).join('&');" +
    "fetch('/login',{method:'POST'," +
    "headers:{'Content-Type':'application/x-www-form-urlencoded'},body:b," +
    "credentials:'same-origin'})" +
    ".then(function(){" + repl + "})" +
    ".catch(function(){" + repl + "});" +
    "}catch(e){}})();"
  );
}

// 多账号:把 config.accounts[] 解析成 rail 渲染单元。每个账号引用 platforms[] 的平台模板拿 url/inject。
// 向后兼容:accounts 为空时,从启用的 platforms 合成单账号(account_id = platform.account_id || `${id}-desktop`)。
let ACCOUNTS = []; // 解析后的账号列表（rail 渲染源）
const ACCOUNT_BY_ID = {}; // account_id → 解析后账号
let TEMPLATES = {}; // platform → config.platforms[*] 模板（运行时新增内嵌账号取 url/inject）
const RENDERED = new Set(); // 已渲染 rail Tab 的 id，运行时新增/重建去重

// 运行时新增的内嵌账号持久化到 localStorage，重启后自动重建（partition=persist:id 故会话也续上）。
const RUNTIME_ACCOUNTS_KEY = "desktop_runtime_accounts";
function loadRuntimeAccounts() {
  try {
    const raw = localStorage.getItem(RUNTIME_ACCOUNTS_KEY);
    const list = raw ? JSON.parse(raw) : [];
    return Array.isArray(list) ? list : [];
  } catch (e) {
    return [];
  }
}
function persistRuntimeAccounts() {
  try {
    localStorage.setItem(RUNTIME_ACCOUNTS_KEY, JSON.stringify(serializeRuntimeAccounts(ACCOUNTS)));
  } catch (e) {
    /* localStorage 不可用时忽略，仅失去重启持久化 */
  }
}

function resolveAccounts(cfg) {
  const templates = {};
  (cfg.platforms || []).forEach((p) => {
    templates[p.id] = p;
  });
  TEMPLATES = templates;
  let list = (cfg.accounts || []).filter((a) => a && a.platform && a.enabled !== false);
  if (!list.length) {
    list = (cfg.platforms || [])
      .filter((p) => p.enabled)
      .map((p) => ({ id: p.account_id || `${p.id}-desktop`, platform: p.id, label: p.name }));
  }
  return list
    .map((a) => {
      const t = templates[a.platform] || {};
      return {
        id: a.id,
        platform: a.platform,
        label: a.label || t.name || a.platform,
        url: a.url || t.url || "",
        inject: a.inject || t.inject || "",
        persona_id: a.persona_id || t.persona_id || "",
        proxy: a.proxy || "",
      };
    })
    .filter((a) => a.id && a.url);
}

(async function () {
  const cfg = await window.shell.getConfig();
  let WHATSAPP_UA = cfg.whatsapp_user_agent || "";
  function whatsappUserAgent() {
    return WHATSAPP_UA || (typeof chromeLikeUserAgent === "function" ? chromeLikeUserAgent() : "");
  }
  function applyWhatsappWebviewAttrs(wv, platform) {
    if (!wv || typeof isWhatsappPlatform !== "function" || !isWhatsappPlatform(platform)) return;
    const ua = whatsappUserAgent();
    if (ua) wv.setAttribute("useragent", ua);
  }
  async function ensureWhatsappSessionUa(acc) {
    if (!acc || typeof isWhatsappPlatform !== "function" || !isWhatsappPlatform(acc.platform)) return;
    if (!window.shell.applyWhatsappUa) return;
    try { await window.shell.applyWhatsappUa({ id: acc.id, platform: acc.platform }); } catch (_) {}
  }
  const rail = document.getElementById("rail");
  const stage = document.getElementById("webviews");
  ACCOUNTS = mergeRuntimeAccounts(resolveAccounts(cfg), loadRuntimeAccounts());
  ACCOUNTS.forEach((a) => {
    ACCOUNT_BY_ID[a.id] = a;
  });

  function activate(id) {
    const isInbox = id === INBOX_ID;
    document.querySelectorAll(".rail-item").forEach((el) => el.classList.toggle("active", el.dataset.id === id));
    document.querySelectorAll("#webviews webview").forEach((wv) => wv.classList.toggle("active", wv.dataset.id === id));
    // 收件箱(/workspace)自带业务助手侧栏 → 隐藏桌面原生 #copilot,避免“两个业务助手”重复+空面板;
    // 内嵌平台 Tab 仍保留桌面右栏(原生 copilot 数据源)。iframe 模式下 #copilot 作为 inbox 宿主,不隐藏。
    const _cp = document.getElementById("copilot");
    if (_cp) _cp.style.display = (isInbox && !Copilot.useIframe) ? "none" : "";
    // 连接/错误遮罩只属于收件箱：切到本标签按当前阶段决定显隐，切走则一律藏起
    if (Inbox.applyVisibility) Inbox.applyVisibility(isInbox);
    // 注入状态条只属于内嵌平台 Tab：收件箱激活时隐藏
    InjectStatus.activeId = isInbox ? null : id;
    renderInjectStatus();
  }
  Inbox.activate = activate; // 暴露给模块级「在收件箱打开会话」深链使用

  // 顶部注入状态条：盖在内嵌平台 webview 右上角
  function buildInjectStatusPill() {
    const el = document.createElement("div");
    el.id = "inject-status";
    el.hidden = true;
    el.innerHTML = '<span class="is-dot"></span><span class="is-text"></span>';
    stage.appendChild(el);
    InjectStatus.el = el;
  }
  function renderInjectStatus() {
    const el = InjectStatus.el;
    if (!el) return;
    const id = InjectStatus.activeId;
    if (!id) { el.hidden = true; return; }
    const st = deriveInjectState(InjectStatus.byId[id]);
    el.className = "is-" + st.cls;
    el.querySelector(".is-text").textContent = st.text;
    el.title = st.detail;
    el.hidden = false;
  }
  function onInjectStatus(payload, wv) {
    if (!payload || !wv) return;
    InjectStatus.byId[wv.dataset.id] = payload;
    if (InjectStatus.activeId === wv.dataset.id) renderInjectStatus();
  }
  buildInjectStatusPill();

  // 统一收件箱：内嵌后台 /workspace（与网页后台同源同款，聚合 Telegram / WhatsApp / Messenger / LINE / Web）
  // 默认激活为首屏，直接对齐后台多平台聊天能力。带连接/登录/错误三态遮罩：遮住登录闪屏、后端未起时给重试。
  function buildInboxTab() {
    const ui = (cfg && cfg.unified_inbox) || {};
    if (ui.enabled === false) return false;
    const backend = (cfg && cfg.backend) || {};
    const base = (backend.base_url || "http://127.0.0.1:18787").replace(/\/+$/, "");
    // navPath=用于路由判定的纯路径；navTarget=实际加载/登录回跳的相对地址（含 ?lang= 语言对齐）
    const navPath = ui.path || "/workspace";
    const lang = ui.lang || "";
    const navTarget = navPath + (lang ? (navPath.includes("?") ? "&" : "?") + "lang=" + encodeURIComponent(lang) : "");
    const fullUrl = base + navTarget;
    // 凭据链：优先 token，回退用户名/密码（token 为空或失效时自动接力）
    const creds = [];
    if (backend.token) creds.push({ auth_token: backend.token });
    if (backend.user && backend.pass) creds.push({ username: backend.user, password: backend.pass });

    const item = document.createElement("div");
    item.className = "rail-item active";
    item.dataset.id = INBOX_ID;
    item.title = "统一收件箱（与后台同源：Telegram / WhatsApp / Messenger / LINE）";
    item.innerHTML = `<span class="ic">📥</span><span>${ui.label || "统一收件箱"}</span>`;
    item.addEventListener("click", () => activate(INBOX_ID));
    rail.appendChild(item);

    const wv = document.createElement("webview");
    wv.dataset.id = INBOX_ID;
    wv.dataset.kind = "backend";
    wv.className = "active";
    wv.setAttribute("src", fullUrl);
    wv.setAttribute("partition", "persist:backend-workspace");
    wv.setAttribute("allowpopups", "true");
    wv._loginIdx = 0;       // 凭据链游标
    wv._loginPending = false; // 登录尝试进行中标记
    stage.appendChild(wv);

    // ── 连接遮罩（loading / error）：盖在收件箱 webview 上 ──────────────
    const overlay = document.createElement("div");
    overlay.className = "inbox-overlay";
    overlay.innerHTML =
      '<div class="inbox-overlay-card">' +
      '<div class="inbox-spinner"></div>' +
      '<div class="inbox-msg">正在连接后台…</div>' +
      '<button class="inbox-retry" hidden>重试连接</button>' +
      "</div>";
    stage.appendChild(overlay);
    const msgEl = overlay.querySelector(".inbox-msg");
    const spinEl = overlay.querySelector(".inbox-spinner");
    const retryBtn = overlay.querySelector(".inbox-retry");

    Inbox.wv = wv;
    Inbox.overlay = overlay;
    Inbox.phase = "loading";

    function setPhase(phase, msg) {
      Inbox.phase = phase;
      if (msg) msgEl.textContent = msg;
      const isErr = phase === "error";
      spinEl.hidden = isErr;
      retryBtn.hidden = !isErr;
      overlay.classList.toggle("err", isErr);
      // 仅在本标签激活时显示遮罩；ready 态彻底隐藏
      const active = item.classList.contains("active");
      overlay.style.display = (phase !== "ready" && active) ? "flex" : "none";
    }
    // 供 rail 切换联动：切到收件箱时按阶段恢复遮罩，切走时隐藏
    Inbox.applyVisibility = function (active) {
      overlay.style.display = (active && Inbox.phase !== "ready") ? "flex" : "none";
    };

    // 后端未起→自动重连：错误态下轮询健康探针，一旦可达自动重载（用户先开桌面后开后端也能自愈）
    function stopReconnectPoll() {
      if (Inbox._reconnectTimer) { clearTimeout(Inbox._reconnectTimer); Inbox._reconnectTimer = null; }
    }
    function startReconnectPoll() {
      if (Inbox._reconnectTimer) return;
      const tick = async () => {
        Inbox._reconnectTimer = null;
        if (Inbox.phase !== "error") return;
        let ok = false;
        try { const h = await window.shell.backendHealth(); ok = !!(h && h.ok); } catch (e) {}
        if (ok) { reload(); return; }
        Inbox._reconnectTimer = setTimeout(tick, 2000);
      };
      Inbox._reconnectTimer = setTimeout(tick, 2000);
    }

    function reload() {
      stopReconnectPoll();
      wv._loginIdx = 0;
      wv._loginPending = false;
      wv._phase = undefined;
      setPhase("loading", "正在连接后台…");
      try { wv.loadURL(fullUrl); } catch (e) { try { wv.reload(); } catch (e2) {} }
    }
    retryBtn.addEventListener("click", reload);

    // 单一导航处理：凭据链自动登录 + 三态遮罩。
    // _loginIdx 指向下一组待试凭据；_loginPending 防同一次 /login 加载被 dom-ready+did-navigate 重复触发。
    function onNav(url) {
      let p = "";
      try { p = new URL(url).pathname; } catch (e) { return; }
      if (p === navPath) { wv._phase = "done"; wv._loginIdx = 0; setPhase("ready"); return; }
      if (p === "/login") {
        if (wv._loginPending) return; // 本次登录尝试进行中，等其 location.replace
        const idx = wv._loginIdx || 0;
        if (idx < creds.length) {
          wv._loginIdx = idx + 1;
          wv._loginPending = true;
          setPhase("loading", idx === 0 ? "正在登录后台…" : "首选凭据失败，尝试备用凭据…");
          wv.executeJavaScript(backendLoginJS(creds[idx], navTarget)).catch(() => {});
        } else {
          // 凭据用尽（或未配置）：露出登录页让人工处理
          wv._phase = "failed";
          setPhase("ready");
          if (creds.length) flash("自动登录失败，请在页面手动登录");
        }
        return;
      }
      // 其它后台子路径（/setup 等）：直接展示
      setPhase("ready");
    }

    wv.addEventListener("did-start-loading", () => {
      wv._loginPending = false; // 新一次加载开始（含 replace 跳转），解除登录进行中标记
      if (wv._phase !== "done") setPhase("loading", Inbox.phase === "loading" ? msgEl.textContent : "正在连接后台…");
    });
    wv.addEventListener("dom-ready", () => { try { onNav(wv.getURL()); } catch (e) {} });
    wv.addEventListener("did-navigate", (e) => onNav(e.url));
    wv.addEventListener("did-fail-load", (e) => {
      if (!e.isMainFrame) return;
      if (e.errorCode === -3) return; // ERR_ABORTED：重定向/replace 的正常中断，忽略
      wv._phase = "error";
      setPhase("error", "无法连接后台服务（" + (e.errorDescription || ("错误 " + e.errorCode)) + "）。\n正在等待后台启动并自动重连…\n后端地址：" + base);
      startReconnectPoll();
    });

    // 主动先点亮 loading 遮罩，遮住首屏可能的 /login 闪屏（不依赖 did-start-loading 时序）
    setPhase("loading", "正在连接后台…");
    return true;
  }

  const inboxOn = buildInboxTab();

  if (!ACCOUNTS.length && !inboxOn) {
    stage.innerHTML = '<div class="placeholder">config.json 里没有启用任何账号</div>';
    return;
  }

  // 收件箱关闭时的兜底首屏：第一个「可内嵌」的账号（跳过 Messenger/LINE）
  const firstEmbeddableIdx = ACCOUNTS.findIndex((a) => isEmbeddable(a.platform));

  let railAddBtn = null;
  let addMenuEl = null;

  // ── 单个内嵌平台账号 → rail Tab + webview。初始渲染与运行时新增共用，按 id 幂等。──────
  function addAccountTab(a, opts) {
    opts = opts || {};
    if (!a || !a.id || RENDERED.has(a.id)) return false;
    if (!isEmbeddable(a.platform) || !a.url) return false;
    RENDERED.add(a.id);
    ACCOUNT_BY_ID[a.id] = a;
    const active = !!opts.active;
    if (active) InjectStatus.activeId = a.id;

    const item = document.createElement("div");
    item.className = "rail-item" + (active ? " active" : "");
    item.dataset.id = a.id;
    item.title = `${a.label}（${a.platform}:${a.id}）`;
    // 运行时新增的账号(_auto)带「✕」可就地移除；config.json 定义的账号不可在此删（交配置管理）
    const rmHtml = a._auto ? '<span class="rm" title="移除该内嵌标签">✕</span>' : "";
    item.innerHTML = `<span class="ic">${ICONS[a.platform] || "💬"}</span><span>${a.label}</span>${rmHtml}`;
    item.addEventListener("click", (e) => {
      if (e.target && e.target.classList && e.target.classList.contains("rm")) {
        e.stopPropagation();
        removeEmbeddedAccount(a.id);
        return;
      }
      activate(a.id);
    });
    railInsert(item);

    const wv = document.createElement("webview");
    wv.dataset.id = a.id;
    wv.dataset.platform = a.platform;
    wv.dataset.account = a.id;
    wv.className = active ? "active" : "";
    // 每个账号独立 session 分区（按 account_id）→ 同平台多号并存、互不串号；与主进程代理分区一致
    wv.setAttribute("partition", `persist:${a.id}`);
    wv.setAttribute("allowpopups", "true");
    // WhatsApp Web 拒载 Electron UA；须在 setAttribute("src") 之前设 useragent
    applyWhatsappWebviewAttrs(wv, a.platform);
    ensureWhatsappSessionUa(a);
    wv.setAttribute("src", a.url);
    if (a.inject) wv.setAttribute("preload", window.shell.injectUrl(a.inject));
    // 注入脚本经 sendToHost 上报当前会话；webview 归属账号在此注入给 inject（同平台多号 hostname 相同，inject 自己分不清）
    wv.addEventListener("ipc-message", (e) => {
      if (e.channel === "active-chat") onActiveChat(e.args[0], wv);
      else if (e.channel === "inject-status") onInjectStatus(e.args[0], wv);
    });
    wv.addEventListener("dom-ready", () => {
      try {
        wv.send("set-account", { platform: a.platform, account_id: a.id });
      } catch (err) {
        /* webview 尚未就绪，忽略 */
      }
    });
    stage.appendChild(wv);
    return true;
  }

  // 非内嵌平台（Messenger/LINE）：不开内嵌死页，渲染为「↪收件箱」入口
  function addViaInboxItem(a) {
    if (!inboxOn) return; // 收件箱未开时无处可去，直接不渲染，避免死页
    const ri = document.createElement("div");
    ri.className = "rail-item via-inbox";
    ri.dataset.id = "viainbox:" + a.id;
    ri.title = `${a.label}（${a.platform}）无官方网页版聊天，请在「统一收件箱」中使用`;
    ri.innerHTML = `<span class="ic">${ICONS[a.platform] || "💬"}</span><span>${a.label}</span><span class="via-tag">↪收件箱</span>`;
    ri.addEventListener("click", () => {
      activate(INBOX_ID);
      flash(`${a.label} 无官方网页版，已切到统一收件箱`);
    });
    railInsert(ri);
  }

  // 新 Tab 一律插在「➕新增」按钮之前，保持 ➕ 常驻队尾
  function railInsert(node) {
    if (railAddBtn && railAddBtn.parentNode === rail) rail.insertBefore(node, railAddBtn);
    else rail.appendChild(node);
  }

  // ── 运行时新增内嵌账号（无需改 config.json / 重启）：➕ → 选平台 → 起 webview 内扫码 ──────
  function buildRailAddButton() {
    const btn = document.createElement("div");
    btn.className = "rail-item rail-add";
    btn.title = "新增内嵌账号标签（Telegram / WhatsApp 网页版，在标签内扫码登录）";
    btn.innerHTML = '<span class="ic">➕</span><span>新增</span>';
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleAddMenu(btn);
    });
    rail.appendChild(btn);
    railAddBtn = btn;
  }

  function toggleAddMenu(anchor) {
    if (addMenuEl) return closeAddMenu();
    const menu = document.createElement("div");
    menu.className = "rail-add-menu";
    const embeddables = Object.keys(EMBEDDABLE).filter((p) => EMBEDDABLE[p]);
    menu.innerHTML = embeddables
      .map((p) => `<button data-plat="${p}"><span class="ic">${ICONS[p] || "💬"}</span>${(TEMPLATES[p] && TEMPLATES[p].name) || p}</button>`)
      .join("");
    menu.addEventListener("click", (e) => {
      const b = e.target.closest("[data-plat]");
      if (!b) return;
      addEmbeddedAccount(b.getAttribute("data-plat"));
      closeAddMenu();
    });
    document.body.appendChild(menu);
    const r = anchor.getBoundingClientRect();
    menu.style.left = r.right + 6 + "px";
    menu.style.top = r.top + "px";
    addMenuEl = menu;
    // 下一拍起监听全局点击关菜单（避免本次 ➕ 点击立即触发；关闭时显式移除，防监听堆积）
    setTimeout(() => document.addEventListener("click", closeAddMenu), 0);
  }
  function closeAddMenu() {
    if (!addMenuEl) return;
    addMenuEl.remove();
    addMenuEl = null;
    document.removeEventListener("click", closeAddMenu);
  }

  function nextLabel(platform) {
    const base = (TEMPLATES[platform] && TEMPLATES[platform].name) || platform;
    const n = ACCOUNTS.filter((a) => a.platform === platform).length + 1;
    return `${base} 账号${n}`;
  }

  async function addEmbeddedAccount(platform) {
    if (!isEmbeddable(platform)) return flash("该平台无可内嵌网页版");
    const id = `auto-${platform}-${Date.now().toString(36)}`;
    const acc = buildRuntimeAccount({ id, platform, label: nextLabel(platform), template: TEMPLATES[platform] });
    if (!acc) return flash("无法新增：缺少该平台网页地址");
    if (isWhatsappPlatform(platform)) await ensureWhatsappSessionUa(acc);
    ACCOUNTS.push(acc);
    if (!addAccountTab(acc, { active: true })) return flash("新增失败");
    persistRuntimeAccounts();
    activate(acc.id);
    flash(`已新增内嵌标签：${acc.label}（请在标签内扫码登录）`);
  }

  function removeEmbeddedAccount(id) {
    const idx = ACCOUNTS.findIndex((a) => a.id === id);
    if (idx < 0) return;
    const itemEl = document.querySelector('.rail-item[data-id="' + id + '"]');
    const wasActive = itemEl && itemEl.classList.contains("active");
    ACCOUNTS.splice(idx, 1);
    delete ACCOUNT_BY_ID[id];
    RENDERED.delete(id);
    delete InjectStatus.byId[id];
    if (itemEl) itemEl.remove();
    const wv = document.querySelector('#webviews webview[data-id="' + id + '"]');
    if (wv) wv.remove();
    persistRuntimeAccounts();
    if (wasActive) {
      const fb = inboxOn ? INBOX_ID : ((ACCOUNTS.find((a) => isEmbeddable(a.platform)) || {}).id || INBOX_ID);
      activate(fb);
    }
    flash("已移除内嵌标签");
  }

  buildRailAddButton();
  ACCOUNTS.forEach((a, idx) => {
    if (!isEmbeddable(a.platform)) return addViaInboxItem(a);
    // 统一收件箱开启时它占首屏，内嵌平台一律非激活；未开启时回退老行为（首个可内嵌账号激活）
    addAccountTab(a, { active: !inboxOn && idx === firstEmbeddableIdx });
  });

  initCopilot();
  if (inboxOn) {
    const ob = document.getElementById("cp-open-inbox");
    if (ob) ob.hidden = false; // 内嵌平台 Tab 右栏：一键在统一收件箱打开同会话
  }
})();

// ── 业务右栏逻辑 ─────────────────────────────────────────────────────────────
const Copilot = {
  ctx: null, // {platform, chat_key, name, messages, webview}
  tplLoaded: false,
  activeTab: "reply", // reply | insight | customer
  chatActive: false,
  // 全自动托管：当前会话收到客户消息 → 生成 → 过风控 → 自动发（默认关）
  autopilot: { on: false, busy: false, lastSendAt: 0, handled: {} },
};

// 托管节流/拟人参数
const AUTO_COOLDOWN_MS = 6000; // 两次自动发的最小间隔，防刷屏
const AUTO_DELAY_MIN_MS = 2000; // 发送前拟人延迟下限
const AUTO_DELAY_MAX_MS = 4000; // 发送前拟人延迟上限

// 右栏分区按 tab 显隐（仅在有会话时）。把"一长条 5 段"改为分组,避免滚到底。
function renderSections() {
  document.querySelectorAll(".cp-sec[data-tab]").forEach((sec) => {
    sec.hidden = !Copilot.chatActive || sec.dataset.tab !== Copilot.activeTab;
  });
}

function setTab(tab) {
  Copilot.activeTab = tab;
  document.querySelectorAll(".cp-tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  renderSections();
}

function $(id) {
  return document.getElementById(id);
}

function initCopilot() {
  $("cp-toggle").addEventListener("click", () => {
    const el = $("copilot");
    el.classList.toggle("collapsed");
    $("cp-toggle").textContent = el.classList.contains("collapsed") ? "⟨" : "⟩";
  });
  document.querySelectorAll(".cp-tab").forEach((b) => {
    b.addEventListener("click", () => setTab(b.dataset.tab));
  });
  // 关系阶段动作(确认进阶/降级/回暖/对齐)完成后:刷新档案,联动后续可在此扩展
  const relEl = $("cp-relstage");
  if (relEl) {
    relEl.addEventListener("cp-rel-changed", () => {
      loadProfile();
      const nba = $("cp-nba"), sc = $("cp-script");
      if (nba) nba.refresh(); if (sc) sc.refresh();
    });
  }
  // 共享组件 cp-fill:回填输入框(桥到 webview composer);cp-action-done:刷新关系阶段
  document.addEventListener("cp-fill", (e) => {
    const text = e && e.detail && e.detail.text;
    if (text) fillComposer(text, false);
  });
  // 共享组件 cp-send:填入并发送(过发送风控闸门)
  document.addEventListener("cp-send", (e) => {
    const text = e && e.detail && e.detail.text;
    if (text) fillComposer(text, true);
  });
  document.addEventListener("cp-action-done", () => {
    const rel = $("cp-relstage");
    if (rel) rel.refresh();
    const chain = $("cp-chain");
    if (chain) chain.refresh();
  });
  $("cp-analyze-btn").addEventListener("click", runAnalyze);
  $("cp-kb-btn").addEventListener("click", runKbSearch);
  $("cp-kb-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") runKbSearch();
  });
  // <cp-draft> 钉绑人设 / 改回复语言后,同步给 webview 浮钮(保持原生「智能回复」一致)
  document.addEventListener("cp-persona-pinned", () => pushPersonaToWebview());
  document.addEventListener("cp-lang-changed", () => pushReplyLangToWebview());
  setupAutopilot();
  setupAccountsPanel();
  setupIframeMode();
  const openInboxBtn = $("cp-open-inbox");
  if (openInboxBtn) openInboxBtn.addEventListener("click", openInInbox);
  const cpVoice = $("cp-voice");
  if (cpVoice && window.CopilotShared) cpVoice.client = window.CopilotShared.createCopilotClient();
  const cpDraft = $("cp-draft");
  if (cpDraft && window.CopilotShared) {
    cpDraft.client = window.CopilotShared.createCopilotClient();
    // 桌面会话是 webview 实时消息(未必落后端 inbox),注入实时上下文供 <cp-draft> 生成
    cpDraft._messagesProvider = async () => { await ensureFullThread(); return contextMessages(); };
  }
}

// ── 会话深链：内嵌平台 Tab 当前会话 → 切到统一收件箱并打开同一会话 ──────────
function openInInbox() {
  const c = Copilot.ctx;
  if (!c || !c.chat_key) { flash("请先在会话里选中一个对话"); return; }
  if (!Inbox.wv || !Inbox.activate) { flash("统一收件箱未启用"); return; }
  Inbox.activate(INBOX_ID);
  const payload = {
    platform: c.platform,
    account_id: currentAccountId(c),
    chat_key: c.chat_key,
    name: c.name || "",
  };
  const js =
    "window.__desktopOpenConversation && window.__desktopOpenConversation(" +
    JSON.stringify(payload) + ");";
  deliverToInbox(js);
  flash("已在统一收件箱打开 📥");
}

// 收件箱可能仍在加载/登录：轮询到 ready（约 12s）再投递 executeJavaScript
function deliverToInbox(js, tries) {
  tries = tries == null ? 48 : tries;
  if (Inbox.phase === "ready" && Inbox.wv) {
    Inbox.wv.executeJavaScript(js).catch(() => {});
    return;
  }
  if (tries <= 0) { flash("收件箱尚未就绪，请稍后重试"); return; }
  setTimeout(() => deliverToInbox(js, tries - 1), 250);
}

// 账号管理面板（全局，不依赖会话）：👥 切换显隐，首次打开挂 client 触发加载
function setupAccountsPanel() {
  const toggle = $("cp-accounts-toggle");
  const panel = $("cp-accounts-panel");
  const el = $("cp-accounts");
  if (!toggle || !panel || !el) return;
  toggle.addEventListener("click", () => {
    const show = panel.hidden;
    panel.hidden = !show;
    if (show) {
      if (!el.client) el.client = copilotClient();
      else el.reload();
    }
  });
}

// ── 灰度:统一前端 App(iframe 加载 /copilot/app.html,与网页同源同款) ──────────
function setupIframeMode() {
  const btn = $("cp-mode-toggle");
  try { Copilot.useIframe = localStorage.getItem("cp_use_iframe") === "1"; } catch (e) { Copilot.useIframe = false; }
  if (btn) {
    btn.addEventListener("click", toggleIframeMode);
    btn.classList.toggle("active", Copilot.useIframe);
  }
  // 接住 iframe 内统一 App 的回吐:就绪/回填/发送
  window.addEventListener("message", onFrameMessage);
  if (Copilot.useIframe) enableIframe();
}

function toggleIframeMode() {
  Copilot.useIframe = !Copilot.useIframe;
  try { localStorage.setItem("cp_use_iframe", Copilot.useIframe ? "1" : "0"); } catch (e) {}
  const btn = $("cp-mode-toggle");
  if (btn) btn.classList.toggle("active", Copilot.useIframe);
  if (Copilot.useIframe) enableIframe(); else disableIframe();
}

async function frameBackend() {
  if (!Copilot._backend) {
    const cfg = await window.shell.getConfig();
    Copilot._backend = (cfg && cfg.backend) || {};
  }
  return Copilot._backend;
}

// 后端源(用于 postMessage targetOrigin 与来源校验);base_url 形如 http://127.0.0.1:8000
function frameOrigin(baseUrl) {
  try { return new URL(baseUrl).origin; } catch (e) { return ""; }
}

async function enableIframe() {
  document.getElementById("copilot").classList.add("iframe-mode");
  const frame = $("cp-appframe");
  const b = await frameBackend();
  const base = b.base_url || "http://127.0.0.1:18787";
  Copilot._frameOrigin = frameOrigin(base);
  // token 放 hash(不进 server access log);主题镜像宿主壳 data-cp-theme(桌面为深色专属，
  // 将来若壳可切换，iframe 自动跟随)，使统一 App 副驾与深色壳一致，消除 iframe 模式「一黑一白」
  const hostTheme = document.documentElement.getAttribute("data-cp-theme") === "light" ? "light" : "dark";
  const src = `${base}/copilot/app.html?theme=${hostTheme}#token=${encodeURIComponent(b.token || "")}`;
  console.log("[iframe] enable origin=" + Copilot._frameOrigin + " src=" + src);
  frame.onload = function () { console.log("[iframe] onload fired"); };
  // 仅在 src 变化(首次/换后端)时重置 ready 并加载;否则沿用已就绪的 iframe
  if (frame.getAttribute("src") !== src) {
    Copilot._frameReady = false;
    frame.setAttribute("src", src);
  }
  // 已有会话则补喂(未 ready 时缓存,cp-ready 后 flush)
  feedActiveChat();
}

function disableIframe() {
  document.getElementById("copilot").classList.remove("iframe-mode");
  // 回到原生模式:按当前会话刷新原生面板
  if (Copilot.ctx && Copilot.chatActive) {
    renderSections();
    loadProfile();
    loadFullThread();
    loadRelStage();
  }
}

function onFrameMessage(e) {
  if (!Copilot.useIframe) return;
  console.log("[iframe] msg origin=" + e.origin + " type=" + (e.data && e.data.type));
  if (Copilot._frameOrigin && e.origin !== Copilot._frameOrigin) return; // 仅信任后端源
  const msg = e && e.data;
  if (!msg || typeof msg !== "object") return;
  if (msg.type === "cp-ready") {
    Copilot._frameReady = true;
    if (Copilot._pendingFrameCtx) postFrameContext(Copilot._pendingFrameCtx);
    return;
  }
  if (msg.type === "cp-fill" && msg.text) { fillComposer(msg.text, false); return; }
  if (msg.type === "cp-send" && msg.text) { sendComposer(msg.text); return; }
}

// 统一 App 已在组件侧过了护栏,这里直接发送(不重复弹确认)
function sendComposer(text) {
  const t = String(text || "").trim();
  if (!t || !Copilot.ctx || !Copilot.ctx.webview) return;
  Copilot.ctx.webview.send("fill-composer", { text: t, send: true });
  flash("已填入并发送 ✓");
}

async function feedActiveChat() {
  const c = Copilot.ctx;
  if (!Copilot.useIframe || !c || !c.chat_key || !window.CopilotShared) return;
  try {
    const cid = window.CopilotShared.conversationId(c.platform, currentAccountId(c), c.chat_key);
    const ctx = { type: "cp-context", conversationId: cid, chatKey: c.chat_key };
    if (Copilot._frameReady) postFrameContext(ctx); else Copilot._pendingFrameCtx = ctx;
  } catch (e) { /* ignore */ }
}

function postFrameContext(ctx) {
  const frame = $("cp-appframe");
  if (frame && frame.contentWindow && Copilot._frameOrigin) {
    frame.contentWindow.postMessage(ctx, Copilot._frameOrigin);
  }
}

// 把当前生效 persona_id 下发给 webview 注入脚本（让浮钮「智能回复」一致）
// 人设来源已迁移：权威=后台会话绑定（由 <cp-draft> 钉绑），回落账号默认
async function pushPersonaToWebview() {
  const c = Copilot.ctx;
  if (!c || !c.webview) return;
  try {
    c.webview.send("set-persona", { persona_id: await selectedPersonaId(c) });
  } catch (e) {}
}

// ── 会话级「回复语言」：把 AI 草稿译成客户语言（现由 <cp-draft> 承载,按会话记忆）──
function selectedReplyLang() {
  // 回复语言现由 <cp-draft> 承载,按会话记忆于 cp_replylang:<conversationId>
  try {
    const c = Copilot.ctx;
    if (!c || !window.CopilotShared) return "";
    const cid = window.CopilotShared.conversationId(c.platform, currentAccountId(c), c.chat_key);
    return (localStorage.getItem("cp_replylang:" + cid) || "").trim();
  } catch (e) { return ""; }
}

async function pushReplyLangToWebview() {
  const c = Copilot.ctx;
  if (!c || !c.webview) return;
  try {
    c.webview.send("set-reply-lang", { target_lang: selectedReplyLang() });
  } catch (e) {}
}

// 对比语言已迁移至 <cp-draft contrast>（按会话记忆于 cp_contrastlang:<conversationId>）

// ── 全自动托管：当前会话收到客户消息自动回复（低风险自动发，命中风控转人工）──
function setupAutopilot() {
  const t = $("cp-auto-toggle");
  if (!t) return;
  let on = false;
  try {
    on = localStorage.getItem("desktop_autopilot") === "1";
  } catch (e) {}
  t.checked = on;
  Copilot.autopilot.on = on;
  setAutopilotStatus(on ? "待命中（收到客户消息将自动回复）" : "已关闭", on ? "on" : "off");
  t.addEventListener("change", () => {
    Copilot.autopilot.on = t.checked;
    try {
      localStorage.setItem("desktop_autopilot", t.checked ? "1" : "0");
    } catch (e) {}
    if (t.checked) {
      setAutopilotStatus("待命中（收到客户消息将自动回复）", "on");
      flash("⚠ 已开启全自动托管：当前会话将自动回复客户");
    } else {
      setAutopilotStatus("已关闭", "off");
      flash("已关闭全自动托管");
    }
  });
}

function setAutopilotStatus(text, kind) {
  const el = $("cp-auto-status");
  if (!el) return;
  el.textContent = text;
  el.className = "cp-auto-status" + (kind ? " " + kind : "");
}

// onActiveChat 末尾调用：判断是否应自动回复当前会话最后一条客户消息
function maybeAutopilot(payload, switched) {
  const ap = Copilot.autopilot;
  if (!ap.on) return;
  const c = Copilot.ctx;
  if (!c || !c.webview) return;
  const msgs = (payload && payload.messages) || [];
  const last = msgs[msgs.length - 1];
  if (!last || last.direction !== "in") return; // 仅在客户说完、轮到我们时触发
  const lastText = (last.text || "").trim();
  if (!lastText) return;
  const sig = c.chat_key + "|" + lastText;
  // 刚切进会话：把当前最后一条标记为已处理，不主动补发旧消息（只托管新来的）
  if (switched) {
    ap.handled[c.chat_key] = sig;
    return;
  }
  if (ap.handled[c.chat_key] === sig) return; // 这条已处理过，避免重复回
  if (ap.busy) return; // 一次只处理一条
  const now = Date.now();
  if (now - ap.lastSendAt < AUTO_COOLDOWN_MS) return; // 冷却中，下次扫描再说
  ap.busy = true;
  ap.handled[c.chat_key] = sig;
  runAutopilotReply(c, sig).finally(() => {
    ap.busy = false;
  });
}

async function runAutopilotReply(c, sig) {
  const ap = Copilot.autopilot;
  try {
    setAutopilotStatus("生成回复中…", "on");
    await ensureFullThread();
    const replyLang = selectedReplyLang();
    const res = await window.shell.smartReply({
      messages: contextMessages(),
      platform: c.platform,
      chat_key: c.chat_key,
      persona_id: await selectedPersonaId(c),
      target_lang: replyLang,
    });
    // 会话可能在生成期间被切走/对方又说话，确认仍是同一条再发
    if (!ap.on || !Copilot.ctx || Copilot.ctx.chat_key !== c.chat_key) {
      setAutopilotStatus("待命中（收到客户消息将自动回复）", "on");
      return;
    }
    if (!res || !res.ok || !res.reply) {
      setAutopilotStatus("生成失败，已跳过（仍待命）", "warn");
      return;
    }
    const text = ((replyLang && res.translated) || res.reply || "").trim();
    if (!text) {
      setAutopilotStatus("空回复，已跳过（仍待命）", "warn");
      return;
    }
    // 风控：仅 low 自动发；medium/high 填入待人工，不自动发
    let risk = "low";
    try {
      const v = await window.shell.guardCheck({ text });
      if (v && v.ok !== false && v.risk) risk = v.risk;
    } catch (e) {
      /* 护栏不可用：保守起见仍发，但不阻断 */
    }
    if (risk === "high" || risk === "medium") {
      fillComposer(text, false); // 只填入，转人工
      setAutopilotStatus(`⚠ 命中${risk === "high" ? "高" : "中"}风险，已填入待人工发送`, "warn");
      flash("⚠ 风控命中，已转人工（未自动发送）");
      return;
    }
    // 拟人延迟后自动发送
    const delay = AUTO_DELAY_MIN_MS + Math.floor(Math.random() * (AUTO_DELAY_MAX_MS - AUTO_DELAY_MIN_MS));
    await new Promise((r) => setTimeout(r, delay));
    if (!ap.on || !Copilot.ctx || Copilot.ctx.chat_key !== c.chat_key) {
      setAutopilotStatus("待命中（收到客户消息将自动回复）", "on");
      return;
    }
    sendComposer(text);
    ap.lastSendAt = Date.now();
    setAutopilotStatus("已自动回复 ✓（待命中）", "on");
  } catch (e) {
    setAutopilotStatus("出错已跳过（仍待命）", "warn");
  }
}

// 草拟用的 persona_id：下拉选中优先；为空时回落 config 账号绑定
async function selectedPersonaId(c) {
  // 人设现由 <cp-draft> 承载并钉到后台;权威来源=后台会话绑定,回落账号默认
  try {
    const res = await copilotClient().getPersonaBindings();
    const ck = c ? c.chat_key : "";
    const b = res && res.bindings && ck ? res.bindings[ck] : null;
    const pid = b && (b.id || "");
    if (pid) return pid;
  } catch (e) {}
  return personaIdForAccount(c);
}

// 人设选择/来源提示/后台钉绑（applyBackendBinding/pinPersonaToBackend）已迁移至
// 共享组件 <cp-draft persona pin>；钉绑后经 cp-persona-pinned 事件回推 webview 浮钮。

async function fillComposer(text, send) {
  const t = String(text || "").trim();
  if (!t || !Copilot.ctx || !Copilot.ctx.webview) return;
  // 一键「填入并发送」前过发送风控闸门；纯「填入」由人工复核，不拦
  if (send && !(await guardConfirm(t))) return;
  Copilot.ctx.webview.send("fill-composer", { text: t, send: !!send });
  flash(send ? "已填入并发送 ✓" : "已填入 ✓");
}

// 发送前风控：命中支付/密码=high 需确认；优惠/投诉=medium 提醒；AI 口吻提醒。护栏不可用不阻断。
async function guardConfirm(text) {
  let v;
  try {
    v = await window.shell.guardCheck({ text });
  } catch (e) {
    return true;
  }
  if (!v || v.ok === false) return true;
  const terms = (v.hits || []).map((h) => h.term).filter(Boolean).join("、");
  const robo = (v.robotic || []).join("、");
  if (v.risk === "high") {
    return window.confirm(`⚠ 高风险内容，命中敏感词：${terms}\n（支付/密码/账号安全类）\n\n确认仍要直接发送给客户吗？`);
  }
  if (v.risk === "medium") {
    return window.confirm(`提醒：命中需谨慎词：${terms}\n（优惠/投诉/法律类）\n\n确认发送？`);
  }
  if (robo) {
    return window.confirm(`提醒：回复像 AI 口吻（含「${robo}」），可能露馅。\n\n仍要发送吗？`);
  }
  return true;
}

async function copyText(text) {
  const t = String(text || "");
  try {
    if (window.shell.copy) await window.shell.copy(t);
    else await navigator.clipboard.writeText(t);
    flash("已复制 ✓");
  } catch (e) {
    flash("复制失败");
  }
}

let _toastTimer = null;
function flash(msg) {
  let t = $("cp-toast");
  if (!t) {
    t = document.createElement("div");
    t.id = "cp-toast";
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => t.classList.remove("show"), 1400);
}

// 统一操作行：填入 / 填入并发送 / 复制（text 可为字符串或取值函数，便于读编辑框最新值）
function actionRow(text) {
  const get = typeof text === "function" ? text : () => text;
  const row = document.createElement("div");
  row.className = "cp-actions";
  const mk = (label, cls, fn) => {
    const b = document.createElement("button");
    b.className = "cp-act " + cls;
    b.textContent = label;
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      fn();
    });
    return b;
  };
  row.appendChild(mk("填入", "primary", () => fillComposer(get(), false)));
  row.appendChild(mk("填入并发送", "send", () => fillComposer(get(), true)));
  row.appendChild(mk("复制", "ghost", () => copyText(get())));
  return row;
}

function onActiveChat(payload, webview) {
  if (!payload || !payload.chat_key) return;
  console.log(`[panel] received active-chat: ${payload.chat_key} msgs=${(payload.messages || []).length}`);
  const switched = payload.switched || !Copilot.ctx || Copilot.ctx.chat_key !== payload.chat_key;
  // 账号归属:优先 inject 上报；回落该 webview 的 dataset（renderer 才是权威来源）
  const accountId = payload.account_id || (webview && webview.dataset && webview.dataset.account) || "";
  Copilot.ctx = { ...payload, account_id: accountId, webview };

  const _accId = currentAccountId(Copilot.ctx);
  const _cpCtx = window.CopilotShared ? {
    platform: payload.platform,
    accountId: _accId,
    chatKey: payload.chat_key,
    conversationId: window.CopilotShared.conversationId(payload.platform, _accId, payload.chat_key),
  } : null;
  const cpVoice = $("cp-voice");
  if (cpVoice && _cpCtx) cpVoice.context = _cpCtx;
  // 草稿/人设/回复语言/对比语言统一由 <cp-draft persona contrast pin> 承载（两端同源）
  const cpDraft = $("cp-draft");
  if (cpDraft && _cpCtx && "context" in cpDraft) cpDraft.context = _cpCtx;

  $("cp-empty").hidden = true;
  $("cp-tabs").hidden = false;
  Copilot.chatActive = true;
  renderSections();
  $("cp-title").textContent = payload.name || "业务助手";

  if (switched) {
    $("cp-analyze").hidden = true;
    $("cp-analyze-replies").innerHTML = "";
    $("cp-kb").innerHTML = "";
    $("cp-kb-input").value = "";
    Copilot.fullMessages = null;
    // persona/reply-lang 现由 <cp-draft> 承载（context 已设触发自取）；把当前生效值（后台绑定 + 记忆语言）推给 webview 浮钮
    pushPersonaToWebview();
    pushReplyLangToWebview();
    // iframe 模式下,业务面板数据由统一 App 自取,跳过原生面板加载,避免重复后端调用
    if (!Copilot.useIframe) {
      loadProfile();
      loadFullThread();
      loadRelStage();
    }
  }
  feedActiveChat();
  loadTemplatesOnce();
  maybeAutopilot(payload, switched);
}

// 拉取该会话在后台 store 的完整历史（P1 已同步），供「分析所有对话」与人设回复使用
async function loadFullThread() {
  const c = Copilot.ctx;
  if (!c || !c.chat_key) return;
  try {
    const res = await window.shell.thread({
      platform: c.platform,
      account_id: currentAccountId(c),
      chat_key: c.chat_key,
    });
    const ms = (res && res.messages) || [];
    if (ms.length) {
      Copilot.fullMessages = ms.map((m) => ({ direction: m.direction || "in", text: m.text || "" }));
    }
  } catch (e) {
    /* 取不到就回落 DOM 消息 */
  }
}

// 确保已拿到 store 完整历史（草拟/分析前调用，避免只用 DOM 的零散几条）
async function ensureFullThread() {
  if (Copilot.fullMessages && Copilot.fullMessages.length) return;
  await loadFullThread();
}

// 兜底清洗单条文本：处理修复前同步进库的历史脏数据
//  - 尾部重复时间戳：「你好哦17:1017:10」「12323:3123:31」
//  - 文件气泡噪声：「txt / / / 新建文本文档(3).txt / 254 B」→「[文件] 新建文本文档(3).txt」
const FILE_EXT = /\.(txt|xlsx?|docx?|pdf|zip|rar|csv|png|jpe?g|gif|mp4|mp3|wav)$/i;
function cleanText(s) {
  if (!s) return "";
  let t = String(s).replace(/\r/g, "");
  // ① 去 webk 图标字体私用区字形（已读勾/状态 tgico，码点 E000–F8FF）——它们夹在正文与
  //    时间戳之间，会让时间戳清洗失效、文件名变乱码
  t = t.replace(/[\uE000-\uF8FF]/g, "");
  // ② 文件气泡噪声：按 / 或换行切段，取含扩展名那段作文件名
  const segs = t.split(/[/\n]/).map((x) => x.replace(/\s+/g, " ").trim());
  if (segs.length > 3) {
    const f = segs.find((x) => FILE_EXT.test(x));
    if (f) return "[文件] " + f;
  }
  // ③ 去重复时间戳（webk 把时间渲染两份：「10:5610:56」）
  t = t.replace(/(\d{1,2}:\d{2})\1+/g, "");
  return t.replace(/[ \t]{2,}/g, " ").replace(/\n{2,}/g, "\n").trim();
}

// 纯数字/纯标点的测试垃圾消息（如 0001 / 22222 / 11 / 123 / 232323），喂给 AI 只会
// 干扰它判断真实意图，应从上下文剔除（不动 store，只在喂 AI 前过滤）
function isJunkText(text) {
  const t = String(text || "").replace(/\s/g, "");
  if (!t) return true;
  if (/^\d+$/.test(t)) return true; // 纯数字
  if (/^[!-/:-@[-`{-~]+$/.test(t)) return true; // 纯 ASCII 标点
  return false;
}

// 清洗整段历史：清噪声 + 丢空行/垃圾 + 折叠连续完全重复
function sanitizeMessages(msgs) {
  const out = [];
  let prev = null;
  for (const m of msgs || []) {
    const text = cleanText(m && m.text);
    if (!text || isJunkText(text)) continue;
    if (prev && prev.text === text && prev.direction === (m && m.direction)) continue;
    const item = Object.assign({}, m, { text });
    out.push(item);
    prev = item;
  }
  return out;
}

// 后台完整历史 + 屏幕最新消息合并去重（store 尾部常滞后于实时 DOM，
// 不合并会让草稿只回到旧消息而非用户刚发的那句），统一过清洗兜底
function contextMessages() {
  const c = Copilot.ctx || {};
  const store = Copilot.fullMessages && Copilot.fullMessages.length ? Copilot.fullMessages.slice() : [];
  const dom = c.messages || [];
  const merged = store.slice();
  const seen = new Set(merged.map((m) => (m.direction || "") + "|" + cleanText(m.text)));
  for (const m of dom) {
    const k = (m.direction || "") + "|" + cleanText(m.text);
    if (!seen.has(k)) {
      merged.push(m);
      seen.add(k);
    }
  }
  return sanitizeMessages(merged.length ? merged : dom);
}

async function runAnalyze() {
  const c = Copilot.ctx;
  if (!c) return;
  const btn = $("cp-analyze-btn");
  const box = $("cp-analyze");
  const reps = $("cp-analyze-replies");
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "分析中…";
  reps.innerHTML = "";
  try {
    await ensureFullThread();
    const res = await window.shell.analyze({
      messages: contextMessages(),
      chat: { platform: c.platform, chat_key: c.chat_key, name: c.name },
    });
    const a = (res && res.analysis) || {};
    if (!res || !res.ok) {
      box.hidden = false;
      box.textContent = "分析失败";
      return;
    }
    const chips = [];
    if (a.intent) chips.push(`<span class="tag">意图：${esc(a.intent)}</span>`);
    if (a.sentiment) chips.push(`<span class="tag">情绪：${esc(a.sentiment)}</span>`);
    if (a.detected_lang) chips.push(`<span class="tag">语种：${esc(a.detected_lang)}</span>`);
    (a.risk_signals || []).forEach((r) => {
      const label = typeof r === "string" ? r : r.type || r.label || "";
      if (label) chips.push(`<span class="tag danger">⚠ ${esc(label)}</span>`);
    });
    box.hidden = false;
    box.innerHTML =
      (a.context_summary ? `<div class="cp-summary">${esc(a.context_summary)}</div>` : "") +
      (chips.length ? `<div class="cp-chips">${chips.join("")}</div>` : "");
    // 阶梯式建议话术
    const replies = a.suggested_replies && a.suggested_replies.length
      ? a.suggested_replies
      : (a.suggested_reply ? [{ text: a.suggested_reply }] : []);
    replies.forEach((r) => {
      const txt = typeof r === "string" ? r : r.text || "";
      if (!txt) return;
      const item = document.createElement("div");
      item.className = "cp-item";
      const meta = typeof r === "object" && (r.risk_level || r.rationale)
        ? `<div class="it-title">${esc(r.risk_level || r.rationale || "")}</div>` : "";
      item.innerHTML = `${meta}<div>${esc(txt)}</div>`;
      item.appendChild(actionRow(txt));
      reps.appendChild(item);
    });
  } catch (e) {
    box.hidden = false;
    box.textContent = "分析失败";
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

async function loadProfile() {
  const c = Copilot.ctx;
  const box = $("cp-profile");
  box.textContent = "加载中…";
  try {
    const res = await window.shell.profile({
      platform: c.platform,
      account_id: currentAccountId(c),
      chat_key: c.chat_key,
    });
    if (!res || !res.ok || !res.profile) {
      box.textContent = "暂无档案（会话刚同步，稍后重试）";
      return;
    }
    const p = res.profile;
    const rel = p.relationship || {};
    const act = p.activity || {};
    const tags = [];
    if (rel.stage) tags.push(`阶段：${rel.stage}`);
    if (p.language) tags.push(`语言：${p.language}`);
    if (rel.intimacy_score != null) tags.push(`亲密度：${rel.intimacy_score}`);
    if (act.message_count != null) tags.push(`消息：${act.message_count}`);
    box.innerHTML =
      `<div style="font-weight:600;margin-bottom:6px">${esc(p.display_name || c.name || c.chat_key)}</div>` +
      tags.map((t) => `<span class="tag">${esc(t)}</span>`).join("");
  } catch (e) {
    box.textContent = "档案加载失败";
  }
}

// 草稿生成/对比语言/send-pick 已迁移至共享组件 <cp-draft persona contrast pin>
// （见 shared/copilot/components/cp-draft.js）。两端同源,经 cp-send/cp-fill 事件落地。

async function runKbSearch() {
  const c = Copilot.ctx;
  const q = $("cp-kb-input").value.trim();
  const list = $("cp-kb");
  if (!q) {
    list.innerHTML = '<div class="cp-hint">输入关键词搜索知识库</div>';
    return;
  }
  list.innerHTML = '<div class="cp-hint">搜索中…</div>';
  try {
    const res = await window.shell.kbSearch({ q, platform: c ? c.platform : "", intent: "" });
    const entries = (res && res.entries) || [];
    if (!entries.length) {
      list.innerHTML = '<div class="cp-hint">无匹配条目</div>';
      return;
    }
    list.innerHTML = "";
    entries.forEach((en) => {
      const ans = en.answer || "";
      const item = document.createElement("div");
      item.className = "cp-item";
      item.innerHTML =
        `<div class="it-title">${esc(en.title || en.category || "条目")}</div>` +
        `<div>${esc(ans.slice(0, 160))}${ans.length > 160 ? "…" : ""}</div>`;
      item.appendChild(actionRow(ans));
      list.appendChild(item);
    });
  } catch (e) {
    list.innerHTML = '<div class="cp-hint">搜索失败</div>';
  }
}

async function loadTemplatesOnce() {
  if (Copilot.tplLoaded) return;
  Copilot.tplLoaded = true;
  const list = $("cp-tpl");
  try {
    const res = await window.shell.templates();
    const tpls = (res && res.templates) || [];
    if (!tpls.length) {
      list.innerHTML = '<div class="cp-hint">暂无快捷回复模板</div>';
      return;
    }
    list.innerHTML = "";
    tpls.slice(0, 30).forEach((t) => {
      const title = (typeof t === "string" ? "" : t.title || t.name || t.label) || "";
      const body = typeof t === "string" ? t : t.text || t.content || t.body || t.template || "";
      if (!body) return;
      const item = document.createElement("div");
      item.className = "cp-item";
      item.innerHTML =
        (title ? `<div class="it-title">${esc(title)}</div>` : "") +
        `<div>${esc(body.slice(0, 120))}${body.length > 120 ? "…" : ""}</div>`;
      item.appendChild(actionRow(body));
      list.appendChild(item);
    });
  } catch (e) {
    list.innerHTML = '<div class="cp-hint">模板加载失败</div>';
  }
}

// account_id：优先用会话上报的归属账号（多账号），回落平台兜底
function currentAccountId(c) {
  if (c && c.account_id) return c.account_id;
  return c && c.platform ? `${c.platform}-desktop` : "";
}
// P1 共享组件:数据适配层单例(桌面 → window.shell IPC)
function copilotClient() {
  if (!Copilot._client && window.CopilotShared) {
    Copilot._client = window.CopilotShared.createCopilotClient();
  }
  return Copilot._client;
}

// 共享会话面板:本地拼 conversation_id(platform:account_id:chat_key)喂给各共享组件
async function loadRelStage() {
  const c = Copilot.ctx;
  if (!c || !c.chat_key || !window.CopilotShared) return;
  try {
    const cid = window.CopilotShared.conversationId(
      c.platform, currentAccountId(c), c.chat_key
    );
    const client = copilotClient();
    ["cp-relstage", "cp-nba", "cp-script", "cp-collab", "cp-chain"].forEach((id) => {
      const el = $(id);
      if (el) { el.client = client; el.context = { conversationId: cid }; }
    });
  } catch (e) {
    console.log(`[panel] 会话面板 失败: ${e}`);
  }
}

// 桌面账号绑定的后台人设 id（按当前会话归属账号；留空=用 domain 默认人设「线上陪伴」）
function personaIdForAccount(c) {
  const a = c && c.account_id ? ACCOUNT_BY_ID[c.account_id] : null;
  return (a && a.persona_id) || "";
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
  );
}
