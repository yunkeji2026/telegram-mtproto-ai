"use strict";
/* 业务助手侧栏 chrome：tab 切换 / 宽度拖拽 / 快捷键 / tab badge（Web + 桌面同源）。
   挂 window.CopilotShared.sidebarChrome；经典脚本，满足 CSP 'self'。 */
(function (root) {
  var TAB_KEY = "ws_cp_tab_v2";
  var TAB_ALIASES = { insight: "customer" };
  var VALID_TABS = ["reply", "customer", "tools"];
  var SIDEBAR_W_KEY = "ws_sidebar_w";
  var SIDEBAR_W_MIN = 280;
  var SIDEBAR_W_MAX = 480;
  var SIDEBAR_W_DEFAULT = 300;

  function normalizeTab(tab) {
    tab = TAB_ALIASES[tab] || tab || "reply";
    return VALID_TABS.indexOf(tab) >= 0 ? tab : "reply";
  }

  function readStoredTab(fallback) {
    try {
      var s = localStorage.getItem(TAB_KEY);
      if (s) return normalizeTab(s);
    } catch (_) {}
    return normalizeTab(fallback || "reply");
  }

  function storeTab(tab) {
    try {
      localStorage.setItem(TAB_KEY, normalizeTab(tab));
    } catch (_) {}
  }

  /** @returns {{ getTab: function, setTab: function, bindClick: function, restore: function }} */
  function createTabController(opts) {
    opts = opts || {};
    var tabSel = opts.tabSelector || ".ws-cp-tab";
    var secSel = opts.sectionSelector || ".ws-cp-sec";
    var active = normalizeTab(opts.initialTab || "reply");
    var useStorage = opts.storage !== false;

    function syncDom() {
      document.querySelectorAll(tabSel).forEach(function (b) {
        b.classList.toggle("active", b.dataset.tab === active);
      });
      document.querySelectorAll(secSel).forEach(function (sec) {
        var on = sec.dataset.tab === active;
        if (sec.classList && sec.classList.contains("ws-cp-sec")) {
          sec.classList.toggle("active", on);
        } else {
          sec.classList.toggle("active", on);
          if ("hidden" in sec) sec.hidden = !on;
        }
      });
    }

    function setTab(tab) {
      active = normalizeTab(tab);
      syncDom();
      if (useStorage) storeTab(active);
      if (typeof opts.onChange === "function") opts.onChange(active);
      return active;
    }

    function bindClick() {
      document.querySelectorAll(tabSel).forEach(function (btn) {
        if (btn.__cpTabBound) return;
        btn.__cpTabBound = true;
        btn.addEventListener("click", function () {
          setTab(btn.dataset.tab);
        });
      });
    }

    function restore() {
      if (useStorage) active = readStoredTab(active);
      syncDom();
      if (typeof opts.onChange === "function") opts.onChange(active);
      return active;
    }

    return {
      getTab: function () {
        return active;
      },
      setTab: setTab,
      bindClick: bindClick,
      restore: restore,
    };
  }

  function applyPanelWidth(el, w, min, max, def) {
    if (!el) return def;
    var n = parseInt(w, 10);
    if (isNaN(n)) n = def;
    n = Math.max(min, Math.min(max, n));
    el.style.width = n + "px";
    return n;
  }

  function readStoredWidth(def) {
    try {
      var v = localStorage.getItem(SIDEBAR_W_KEY);
      if (v != null && v !== "") return parseInt(v, 10);
    } catch (_) {}
    return def;
  }

  function storeWidth(w) {
    try {
      localStorage.setItem(SIDEBAR_W_KEY, String(w));
    } catch (_) {}
  }

  /** @returns {{ applyWidth: function, init: function }} */
  function createResizeController(opts) {
    opts = opts || {};
    var panel = typeof opts.panel === "string" ? document.getElementById(opts.panel) : opts.panel;
    var handle = typeof opts.handle === "string" ? document.getElementById(opts.handle) : opts.handle;
    var min = opts.min != null ? opts.min : SIDEBAR_W_MIN;
    var max = opts.max != null ? opts.max : SIDEBAR_W_MAX;
    var def = opts.defaultWidth != null ? opts.defaultWidth : SIDEBAR_W_DEFAULT;
    var storageKey = opts.storageKey || SIDEBAR_W_KEY;
    var hideWhen = opts.hideWhenCollapsed;

    function applyWidth(w) {
      if (!panel) return def;
      return applyPanelWidth(panel, w, min, max, def);
    }

    function init() {
      if (!panel || !handle || handle.__cpResizeBound) return;
      handle.__cpResizeBound = true;
      applyWidth(readStoredWidth(def));
      var dragging = false;
      var startX = 0;
      var startW = 0;

      function onMove(e) {
        if (!dragging || !panel) return;
        var dx = startX - (e.clientX || 0);
        applyWidth(startW + dx);
      }

      function onUp() {
        if (!dragging) return;
        dragging = false;
        handle.classList.remove("dragging");
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        try {
          localStorage.setItem(storageKey, String(parseInt(panel.offsetWidth, 10) || def));
        } catch (_) {}
      }

      handle.addEventListener("mousedown", function (e) {
        if (hideWhen && hideWhen()) return;
        if (e.button !== 0) return;
        dragging = true;
        startX = e.clientX;
        startW = panel.offsetWidth;
        handle.classList.add("dragging");
        document.body.style.cursor = "col-resize";
        document.body.style.userSelect = "none";
        document.addEventListener("mousemove", onMove);
        document.addEventListener("mouseup", onUp);
        e.preventDefault();
      });

      handle.addEventListener("dblclick", function () {
        applyWidth(def);
        try {
          localStorage.setItem(storageKey, String(def));
        } catch (_) {}
      });
    }

    return { applyWidth: applyWidth, init: init };
  }

  function bindTabHotkeys(opts) {
    opts = opts || {};
    if (root.document && root.document.__cpTabHotkeys) return;
    if (root.document) root.document.__cpTabHotkeys = true;
    var tabs = opts.tabs || VALID_TABS;
    document.addEventListener("keydown", function (e) {
      if (!(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey) return;
      if (typeof opts.isEnabled === "function" && !opts.isEnabled()) return;
      var t = e.target;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      var idx = ["1", "2", "3"].indexOf(e.key);
      if (idx < 0 || idx >= tabs.length) return;
      if (typeof opts.setTab === "function") opts.setTab(tabs[idx]);
      e.preventDefault();
    });
  }

  function applyTabBadge(el, text, tone) {
    if (!el) return;
    var t = text || "";
    if (t.length > 14) t = t.slice(0, 14) + "…";
    el.textContent = t;
    el.className = (el.classList.contains("ws-cp-tab-badge") ? "ws-cp-tab-badge" : "cp-tab-badge") +
      (tone === "warn" ? " warn" : tone === "accent" ? " accent" : "");
  }

  function panelSuffix(panelId) {
    return String(panelId || "").replace(/^ws-cp-/, "").replace(/^cp-/, "");
  }

  function _trunc(s, n) {
    s = String(s || "");
    return s.length > n ? s.slice(0, n) + "…" : s;
  }

  /** cp-data-loaded → 卡头 pill 摘要（Web + 桌面同源；opts.t/tf 走 i18n） */
  function pillMetaFromCpLoaded(det, opts) {
    opts = opts || {};
    var t = opts.t || function (k) {
      return k;
    };
    var tf = opts.tf || function (k, v) {
      var s = t(k);
      if (v) {
        Object.keys(v).forEach(function (p) {
          s = s.split("{" + p + "}").join(String(v[p]));
        });
      }
      return s;
    };
    var withJump = opts.jump !== false;
    var pid = String(det && det.panelId || "");
    var suf = panelSuffix(pid);
    var d = det && det.data;
    var tone = "accent";
    var text = "";
    if (!det || !det.ok || !d) return { text: "", tone: "" };

    if (suf === "draft") {
      if (!d.generated) return { text: "", tone: "" };
      if (d.guardRisk === "high") {
        return {
          text: t("inbox.pill.guard_high"),
          tone: "danger",
          jump: withJump ? { card: "draft", tab: "reply", titleKey: "inbox.pill.jump_draft_t" } : null,
        };
      }
      if (d.guardRisk === "medium") {
        return {
          text: t("inbox.pill.guard_medium"),
          tone: "warn",
          jump: withJump ? { card: "draft", tab: "reply", titleKey: "inbox.pill.jump_draft_t" } : null,
        };
      }
      if (d.intent) text = _trunc(d.intent, 16);
      else if (d.persona) text = _trunc(d.persona, 14);
      else text = t("inbox.pill.draft_ready");
      tone = "ok";
    } else if (suf === "persona") {
      text = d.boundName || (d.boundId ? String(d.boundId).slice(0, 10) : "");
    } else if (suf === "relstage") {
      var s = d.display_stage_label || d.stage_label || "";
      var pct = Math.max(0, Math.min(100, Math.round(d.progress_pct || 0)));
      text = s ? s + " · " + pct + "%" : "";
      if (d.pending_advancement || d.needs_confirmation || d.stage_conflict) tone = "warn";
      else if (d.reunion) tone = "danger";
    } else if (suf === "nba") {
      var acts = d.actions || [];
      if (!acts.length) return { text: "", tone: "" };
      var a0 = acts[0];
      text = _trunc(a0.name || "", 16);
      if (a0.action_type === "escalate") tone = "danger";
      else if (a0.action_type === "template") tone = "ok";
    } else if (suf === "chain") {
      var ex = d.executions || [];
      var run = 0;
      var fail = 0;
      ex.forEach(function (x) {
        if (x.status === "running") run++;
        if (x.status === "failed") fail++;
      });
      if (run) {
        text = tf("inbox.pill.chain_running", { n: run });
        tone = "ok";
      } else if (fail) {
        text = tf("inbox.pill.chain_failed", { n: fail });
        tone = "danger";
      } else text = ex.length ? String(ex.length) : "";
    } else if (suf === "script") {
      if (d.stage_label || d.stage) text = d.stage_label || d.stage;
      else {
        var tn = (d.topics || []).length;
        text = tn ? tf("inbox.pill.topics", { n: tn }) : "";
      }
    } else if (suf === "collab") {
      var rel = d.relationship || {};
      text = d.contact_stage_label || rel.display_stage_label || rel.stage_label || "";
      if (d.stage_conflict) tone = "warn";
    }
    return { text: text, tone: text ? tone : "" };
  }

  /** pill 摘要 → tab badge（桌面无 pill DOM 时用；Web 也可复用规则） */
  function tabBadgeFromPillMeta(meta, pid) {
    if (!meta || !meta.text) return null;
    var suf = panelSuffix(pid);
    if (suf === "draft" && (meta.tone === "danger" || meta.tone === "warn")) {
      return { tab: "reply", text: meta.text, tone: meta.tone };
    }
    if (suf === "relstage") {
      if (meta.tone === "danger") return { tab: "customer", text: meta.text, tone: meta.tone };
      if (meta.tone === "warn") return { tab: "customer", text: meta.text, tone: meta.tone };
      return null;
    }
    if (suf === "chain") {
      if (meta.tone === "danger") return { tab: "tools", text: meta.text, tone: meta.tone };
      if (meta.tone === "ok") return { tab: "tools", text: meta.text, tone: meta.tone };
      return null;
    }
    return null;
  }

  /** 从 cp-data-loaded 事件提取 tab badge 摘要（委托 pillMetaFromCpLoaded） */
  function badgeMetaFromLoaded(det, t) {
    t = t || function (k, vars) {
      return k;
    };
    var meta = pillMetaFromCpLoaded(det, {
      t: function (k) {
        return t(k);
      },
      tf: function (k, v) {
        return t(k, v);
      },
      jump: false,
    });
    return tabBadgeFromPillMeta(meta, det && det.panelId);
  }

  /** 桌面壳：监听 cp-data-loaded，刷新 customer/tools/reply tab badge */
  function initDesktopTabBadges(opts) {
    opts = opts || {};
    var badgeReply = opts.badgeReply ? document.getElementById(opts.badgeReply) : null;
    var badgeCustomer = opts.badgeCustomer ? document.getElementById(opts.badgeCustomer) : null;
    var badgeTools = opts.badgeTools ? document.getElementById(opts.badgeTools) : null;
    var state = { reply: null, customer: null, tools: null };
    var snoozed = opts.snoozedLabel || "Snoozed";

    function t(key, vars) {
      if (typeof opts.translate === "function") return opts.translate(key, vars);
      return key;
    }

    function render() {
      function show(name, st) {
        if (!st || !st.text) return false;
        if (name === "tools" || name === "reply") return st.tone === "danger" || st.tone === "warn";
        return true;
      }
      applyTabBadge(badgeReply, show("reply", state.reply) ? state.reply.text : "", show("reply", state.reply) ? state.reply.tone : "");
      applyTabBadge(badgeCustomer, show("customer", state.customer) ? state.customer.text : "", show("customer", state.customer) ? state.customer.tone : "");
      applyTabBadge(badgeTools, show("tools", state.tools) ? state.tools.text : "", show("tools", state.tools) ? state.tools.tone : "");
    }

    function setSnoozed(on) {
      if (on) state.tools = { text: snoozed, tone: "warn" };
      else if (state.tools && state.tools.text === snoozed) state.tools = null;
      render();
    }

    if (root.document && !root.document.__cpDesktopBadgeBound) {
      root.document.__cpDesktopBadgeBound = true;
      document.addEventListener("cp-data-loaded", function (e) {
        var m = badgeMetaFromLoaded(e && e.detail, function (key, vars) {
          return t(key, vars);
        });
        if (m && m.tab) state[m.tab] = { text: m.text, tone: m.tone };
        render();
      });
    }

    return { setSnoozed: setSnoozed, refresh: render, clear: function () {
      state = { reply: null, customer: null, tools: null };
      render();
    } };
  }

  /* ── 线性 SVG 图标集（currentColor，明暗自适应）：卡头 data-cp-ic 用，与网页端同风格 ── */
  var UI_ICONS = {
    search: '<circle cx="11" cy="11" r="7"/><line x1="20.5" y1="20.5" x2="16.6" y2="16.6"/>',
    user: '<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
    users: '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    msg: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    tag: '<path d="M20.6 13.4 12 22l-9-9V3h10l7.6 7.6a2 2 0 0 1 0 2.8z"/><circle cx="7.6" cy="7.6" r="1.3"/>',
    mic: '<rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10v1a7 7 0 0 0 14 0v-1"/><line x1="12" y1="18" x2="12" y2="22"/>',
    spark: '<path d="M11 3l1.7 4.6L17 9l-4.3 1.4L11 15l-1.7-4.6L5 9l4.3-1.4L11 3z"/>',
    zap: '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
    heart: '<path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.6l-1-1a5.5 5.5 0 0 0-7.8 7.8l1 1L12 21l7.8-7.6 1-1a5.5 5.5 0 0 0 0-7.8z"/>',
    link: '<path d="M10 13a5 5 0 0 0 7.5 0l2-2a5 5 0 0 0-7.1-7.1l-1.3 1.3"/><path d="M14 11a5 5 0 0 0-7.5 0l-2 2a5 5 0 0 0 7.1 7.1l1.3-1.3"/>',
    brain: '<path d="M9.5 2A5.5 5.5 0 0 0 4 7.5c0 1.9.9 3.6 2.3 4.7L6 22h12l-.3-9.8A5.5 5.5 0 0 0 14.5 2 5.5 5.5 0 0 0 9.5 2z"/>',
    folder: '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>',
    film: '<rect x="2" y="2" width="20" height="20" rx="2.5"/><line x1="7" y1="2" x2="7" y2="22"/><line x1="17" y1="2" x2="17" y2="22"/>',
    edit: '<path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.1 2.1 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>',
    route: '<circle cx="6" cy="19" r="3"/><path d="M9 19h8.5a3.5 3.5 0 0 0 0-7H11a3.5 3.5 0 0 1 0-7H4"/>',
    book: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>',
    clock: '<circle cx="12" cy="12" r="9"/><polyline points="12 7.5 12 12 15 13.8"/>',
    archive: '<rect x="3" y="4" width="18" height="4" rx="1"/><path d="M5 8v11a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8"/><line x1="10" y1="12" x2="14" y2="12"/>',
  };

  function uiIcon(name, size, cls) {
    var p = UI_ICONS[name];
    if (!p) return "";
    var s = size || 16;
    return '<svg class="ui-ic' + (cls ? " " + cls : "") + '" viewBox="0 0 24 24" width="' + s +
      '" height="' + s + '" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + p + "</svg>";
  }

  /** 把 [data-cp-ic] 节点填成线性 SVG（幂等，__icDone 去重） */
  function decorateCardIcons(rootEl, size) {
    var scope = rootEl || (root.document || document);
    if (!scope || !scope.querySelectorAll) return;
    scope.querySelectorAll("[data-cp-ic]").forEach(function (sp) {
      if (sp.__icDone) return;
      sp.__icDone = 1;
      var ic = uiIcon(sp.getAttribute("data-cp-ic"), size || 14);
      if (ic) sp.innerHTML = ic;
    });
  }

  /* ── 可折叠卡片控制器：折叠态 localStorage 记忆 + 展开懒取数（Web + 桌面同源） ──
     opts: { storageKey, defaultCollapsed:{name:1}, cardSelector, toggleAttr, onExpand(name) } */
  function createCardController(opts) {
    opts = opts || {};
    var storageKey = opts.storageKey || "ws_cp_cards_v1";
    var defaults = opts.defaultCollapsed || {};
    var cardSel = opts.cardSelector || "[data-cp-card]";
    var toggleAttr = opts.toggleAttr || "data-cp-toggle";
    var onExpand = typeof opts.onExpand === "function" ? opts.onExpand : function () {};

    function readState() {
      try {
        return JSON.parse(localStorage.getItem(storageKey) || "{}") || {};
      } catch (_) {
        return {};
      }
    }

    function writeState(st) {
      try {
        localStorage.setItem(storageKey, JSON.stringify(st));
      } catch (_) {}
    }

    function isCollapsed(name) {
      var st = readState();
      return name in st ? !!st[name] : !!defaults[name];
    }

    function apply() {
      document.querySelectorAll(cardSel).forEach(function (card) {
        card.classList.toggle("collapsed", isCollapsed(card.getAttribute("data-cp-card")));
      });
    }

    function toggle(name) {
      var card = document.querySelector('[data-cp-card="' + name + '"]');
      if (!card) return;
      var nowCollapsed = !card.classList.contains("collapsed");
      card.classList.toggle("collapsed", nowCollapsed);
      var st = readState();
      st[name] = nowCollapsed ? 1 : 0;
      writeState(st);
      if (!nowCollapsed) onExpand(name);
    }

    function expand(name) {
      var card = document.querySelector('[data-cp-card="' + name + '"]');
      if (!card) return;
      if (card.classList.contains("collapsed")) toggle(name);
      else onExpand(name);
    }

    function bindClicks(rootArg) {
      var host = (typeof rootArg === "string" ? document.getElementById(rootArg) : rootArg) || document;
      if (host.__cpCardBound) return;
      host.__cpCardBound = true;
      host.addEventListener("click", function (e) {
        var t = e.target.closest && e.target.closest("[" + toggleAttr + "]");
        if (t && (host === document || host.contains(t))) toggle(t.getAttribute(toggleAttr));
      });
    }

    return {
      readState: readState,
      isCollapsed: isCollapsed,
      apply: apply,
      toggle: toggle,
      expand: expand,
      bindClicks: bindClicks,
    };
  }

  root.CopilotShared = Object.assign(root.CopilotShared || {}, {
    sidebarChrome: {
      TAB_KEY: TAB_KEY,
      TAB_ALIASES: TAB_ALIASES,
      VALID_TABS: VALID_TABS,
      SIDEBAR_W_KEY: SIDEBAR_W_KEY,
      SIDEBAR_W_MIN: SIDEBAR_W_MIN,
      SIDEBAR_W_MAX: SIDEBAR_W_MAX,
      SIDEBAR_W_DEFAULT: SIDEBAR_W_DEFAULT,
      normalizeTab: normalizeTab,
      readStoredTab: readStoredTab,
      storeTab: storeTab,
      createTabController: createTabController,
      createResizeController: createResizeController,
      bindTabHotkeys: bindTabHotkeys,
      applyTabBadge: applyTabBadge,
      panelSuffix: panelSuffix,
      pillMetaFromCpLoaded: pillMetaFromCpLoaded,
      tabBadgeFromPillMeta: tabBadgeFromPillMeta,
      badgeMetaFromLoaded: badgeMetaFromLoaded,
      initDesktopTabBadges: initDesktopTabBadges,
      UI_ICONS: UI_ICONS,
      uiIcon: uiIcon,
      decorateCardIcons: decorateCardIcons,
      createCardController: createCardController,
    },
  });
})(typeof window !== "undefined" ? window : globalThis);

if (typeof module !== "undefined" && module.exports) {
  module.exports = (typeof window !== "undefined" ? window : globalThis).CopilotShared.sidebarChrome;
}
