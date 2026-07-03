"use strict";
/* 两端共享 · 账号管理面板 cp-accounts
   统一清单(GET /api/accounts) + 扫码登录(/api/platforms/.../login/*) + 编排器启停。
   自含生命周期(列表/加号/QR 轮询),不依赖会话上下文,故不继承 CpPanelBase。
   用法:el.client = CopilotShared.createCopilotClient(); el.reload();
   挂 window.CopilotShared 无依赖,经典脚本满足 CSP 'self'。 */
(function (root) {
  const PLATFORMS = [
    { id: "telegram", name: "Telegram", icon: "✈️" },
    { id: "whatsapp", name: "WhatsApp", icon: "🟢" },
    { id: "messenger", name: "Messenger", icon: "💠" },
    { id: "line", name: "LINE", icon: "💬" },
  ];
  const PLAT_NAME = {};
  const PLAT_ICON = {};
  PLATFORMS.forEach((p) => {
    PLAT_NAME[p.id] = p.name;
    PLAT_ICON[p.id] = p.icon;
  });
  const MODE_KEY = { protocol: "cp.acct.mode.protocol", web: "cp.acct.mode.web", device: "cp.acct.mode.device", desktop: "cp.acct.mode.desktop" };
  const STATUS_KEY = { online: "cp.acct.status.online", offline: "cp.acct.status.offline", pending: "cp.acct.status.pending", unknown: "cp.acct.status.unknown" };
  function T(key, vars) {
    const f = root.CopilotShared && root.CopilotShared.t;
    return f ? f(key, vars) : key;
  }

  const CSS = `
    :host { display:block; font-size:var(--cp-fs,13px); color:var(--cp-text,#1e293b); }
    .wrap { background:var(--cp-surface,#fff); border:1px solid var(--cp-border,#e2e8f0);
            border-radius:var(--cp-radius,10px); padding:var(--cp-gap,10px); }
    .head { display:flex; align-items:center; gap:6px; margin-bottom:8px; flex-wrap:wrap; }
    .head .t { font-weight:600; flex:1 0 100%; margin-bottom:2px; }
    .empty,.err { color:var(--cp-text-tiny,#94a3b8); font-size:var(--cp-fs-sm,12px); }
    .err { color:var(--cp-danger,#dc2626); }
    button { font:inherit; font-size:var(--cp-fs-tiny,11px); cursor:pointer; white-space:nowrap;
             border:1px solid var(--cp-border,#e2e8f0); background:var(--cp-surface,#fff);
             color:var(--cp-text,#1e293b); border-radius:var(--cp-radius-sm,6px); padding:3px 9px; }
    button.primary { background:var(--cp-accent,#4f46e5); color:#fff; border-color:transparent; }
    button.danger { color:var(--cp-danger,#dc2626); border-color:var(--cp-danger,#dc2626); }
    button:disabled { opacity:.5; cursor:default; }
    .row { display:flex; align-items:center; gap:8px; padding:7px 4px 7px 8px; border-left:3px solid var(--row-accent,transparent); border-bottom:1px solid var(--cp-border,#eef2f7); flex-wrap:wrap; }
    .row:last-child { border-bottom:0; }
    .row .ic { font-size:16px; display:inline-flex; align-items:center; line-height:0; }
    /* 身份化头像：账号色渐变底 + 字首 + 平台品牌角标（同平台共享品牌左强调，账号靠头像区分） */
    .row .av { position:relative; flex-shrink:0; width:34px; height:34px; border-radius:50%;
               display:flex; align-items:center; justify-content:center; color:#fff; font-weight:700;
               font-size:14px; line-height:1; box-shadow:0 1px 2px rgba(0,0,0,.2); user-select:none; }
    .row .av .b { position:absolute; right:-3px; bottom:-3px; width:15px; height:15px; border-radius:50%;
               background:var(--cp-surface,#fff); display:flex; align-items:center; justify-content:center;
               line-height:0; box-shadow:0 0 0 1.5px var(--cp-surface,#fff); }
    .row .av .b svg { width:13px; height:13px; display:block; border-radius:2px; }
    .row .meta { flex:1; min-width:120px; }
    .row .label { font-weight:600; }
    .row .sub { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); }
    .badge { font-size:var(--cp-fs-tiny,11px); padding:1px 7px; border-radius:10px;
             background:var(--cp-surface-2,#f1f5f9); color:var(--cp-text-dim,#64748b); }
    .badge.on { background:rgba(47,158,110,.18); color:#2f9e6e; }
    .badge.off { background:rgba(120,130,150,.18); color:#8a9bb0; }
    .badge.pending { background:rgba(255,176,32,.2); color:#c98a14; }
    .add { margin-top:10px; border-top:1px dashed var(--cp-border,#e2e8f0); padding-top:10px; }
    .add .field { display:flex; align-items:center; gap:6px; margin-bottom:6px; }
    .add label { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); flex:0 0 56px; }
    select,input { font:inherit; font-size:var(--cp-fs-sm,12px); padding:4px 6px; flex:1; min-width:0;
            border:1px solid var(--cp-border,#e2e8f0); border-radius:var(--cp-radius-sm,6px);
            background:var(--cp-surface,#fff); color:var(--cp-text,#1e293b); }
    .qr { text-align:center; margin-top:8px; }
    .qr img { width:180px; height:180px; background:#fff; border-radius:8px; padding:6px; }
    .qr .inst { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); margin-top:6px; white-space:pre-wrap; }
    .qr .st { font-size:var(--cp-fs-sm,12px); margin-top:6px; }
    .qr .st.ok { color:#2f9e6e; }
    .qr .st.fail { color:var(--cp-danger,#dc2626); }
    .audit { margin-top:10px; border-top:1px dashed var(--cp-border,#e2e8f0); padding-top:10px; }
    .audit .sum { display:flex; align-items:center; gap:8px; font-size:var(--cp-fs-tiny,11px);
            color:var(--cp-text-dim,#64748b); margin-bottom:6px; }
    .audit .sum button { margin-left:auto; }
    .audit .it { padding:5px 4px; border-bottom:1px solid var(--cp-border,#eef2f7); }
    .audit .it:last-child { border-bottom:0; }
    .audit .it .hd { display:flex; gap:6px; align-items:center; font-size:var(--cp-fs-tiny,11px);
            color:var(--cp-text-dim,#64748b); }
    .audit .it .tx { font-size:var(--cp-fs-sm,12px); margin-top:2px; white-space:pre-wrap; word-break:break-word; }
    .rb { font-size:var(--cp-fs-tiny,11px); padding:1px 6px; border-radius:8px;
            background:var(--cp-surface-2,#f1f5f9); color:var(--cp-text-dim,#64748b); }
    .rb.sent { background:rgba(47,158,110,.18); color:#2f9e6e; }
    .rb.handoff { background:rgba(220,38,38,.16); color:var(--cp-danger,#dc2626); }
    .live { font-size:var(--cp-fs-tiny,11px); color:#2f9e6e; margin-left:auto; }`;

  class CpAccounts extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: "open" });
      this.shadowRoot.innerHTML =
        `<style>${CSS}</style><div class="wrap"><div class="empty">${T("cp.common.loading")}</div></div>`;
      this._client = null;
      this._poll = null; // 登录轮询计时器
      this._login = null; // {platform, login_id}
      this.shadowRoot.addEventListener("click", (e) => {
        const b = e.target.closest("[data-act]");
        if (b && !b.disabled) this._onAction(b.getAttribute("data-act"), b);
      });
    }

    set client(c) { this._client = c; this.reload(); }
    get client() { return this._client; }
    disconnectedCallback() { this._stopPoll(); this._stopAuditLive(); }

    _wrap() { return this.shadowRoot.querySelector(".wrap"); }
    _esc(s) {
      return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    }

    async reload() {
      this._stopAuditLive();
      if (!this._client) return;
      let d;
      try {
        d = await this._client.listAccounts();
      } catch (e) {
        this._wrap().innerHTML = `<div class="err">${this._esc(T("cp.acct.list_err"))}</div>`;
        return;
      }
      const accs = (d && d.accounts) || [];
      // P5：脱敏开关随 /api/accounts 下发（缺省 true，与 web 端口径一致）
      this._maskPhone = !(d && d.mask_phone === false);
      this._render(accs);
    }

    _render(accs) {
      this._accounts = accs || [];
      const rows = accs.map((a) => this._rowHtml(a)).join("") ||
        `<div class="empty">${this._esc(T("cp.acct.empty"))}</div>`;
      this._wrap().innerHTML =
        `<div class="head"><span class="t">${this._esc(T("cp.acct.title"))}</span>` +
        `<button data-act="health">${this._esc(T("cp.acct.health"))}</button>` +
        `<button data-act="settings">${this._esc(T("cp.acct.settings"))}</button>` +
        `<button data-act="webhooks">${this._esc(T("cp.acct.webhooks"))}</button>` +
        `<button data-act="audit">${this._esc(T("cp.acct.audit_btn"))}</button>` +
        `<button data-act="refresh">${this._esc(T("cp.common.refresh"))}</button>` +
        `<button class="primary" data-act="add">${this._esc(T("cp.acct.add"))}</button></div>` +
        `<div class="list">${rows}</div>` +
        `<div class="form"></div>`;
    }

    _grad(seed) {
      const G = [["#60a5fa", "#2563eb"], ["#38bdf8", "#0284c7"], ["#34d399", "#059669"],
        ["#a78bfa", "#7c3aed"], ["#f472b6", "#db2777"], ["#fbbf24", "#d97706"],
        ["#22d3ee", "#0891b2"], ["#818cf8", "#4f46e5"], ["#fb7185", "#e11d48"],
        ["#2dd4bf", "#0d9488"], ["#c084fc", "#9333ea"], ["#4ade80", "#16a34a"]];
      const s = String(seed || "?"); let h = 0;
      for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
      const g = G[h % G.length];
      return "linear-gradient(135deg," + g[0] + " 0%," + g[1] + " 100%)";
    }

    _selfName(a) {
      return a.self_name || (a.self_username ? "@" + a.self_username : "");
    }

    _maskId(id) {
      // 显示层脱敏手机号（前3+****+后3），与 web 端一致；不改真实 account_id（动作仍用原值）
      // P5：受 mask_phone 开关约束（默认开），关闭则原样显示号码
      const s = String(id == null ? "" : id);
      const m = s.match(/^(\+?)(\d{7,})$/);
      if (m && this._maskPhone !== false) {
        const d = m[2]; return m[1] + d.slice(0, 3) + "****" + d.slice(-3);
      }
      return s;
    }

    _avatarHtml(a) {
      const nm = PLAT_NAME[a.platform] || a.platform || "?";
      const badge = (window.platformIconSVG) ? window.platformIconSVG(a.platform, { size: 13, inlineDefs: true }) : "";
      const badgeHtml = badge ? `<span class="b">${badge}</span>` : "";
      // P1：有真头像用真图，否则回落渐变字首
      if (a.self_avatar) {
        return `<span class="av" style="background:var(--cp-surface-2,#f1f5f9)">` +
          `<img src="${this._esc(a.self_avatar)}" alt="" style="width:100%;height:100%;object-fit:cover;border-radius:50%" onerror="this.remove()">` +
          badgeHtml + `</span>`;
      }
      let ini = String(a.label || this._selfName(a) || "").trim().charAt(0) || String(nm).charAt(0);
      ini = ini.toUpperCase();
      return `<span class="av" style="background:${this._grad(a.platform + ":" + a.account_id)}"><span>${this._esc(ini)}</span>` +
        badgeHtml + `</span>`;
    }

    _rowHtml(a) {
      const accent = (window.platformBrandColor) ? window.platformBrandColor(a.platform) : "transparent";
      const st = a.running ? "online" : (a.status || "unknown");
      const stCls = st === "online" ? "on" : (st === "pending" ? "pending" : "off");
      const stTxt = STATUS_KEY[st] ? T(STATUS_KEY[st]) : st;
      const mode = MODE_KEY[a.mode] ? T(MODE_KEY[a.mode]) : (a.mode || "");
      // 仅 protocol 号可由编排器启停 + 切 7×24 自动回复（web/device 由各自宿主或手机负责）
      const p = this._esc(a.platform);
      const id = this._esc(a.account_id);
      const ctrl = a.mode === "protocol"
        ? `<button data-act="toggle-auto" class="${a.auto_reply ? "primary" : ""}" data-p="${p}" data-a="${id}" data-v="${a.auto_reply ? "1" : "0"}">${this._esc(a.auto_reply ? T("cp.acct.auto_on") : T("cp.acct.auto_off"))}</button>` +
          `<button data-act="adv" data-p="${p}" data-a="${id}">${this._esc(T("cp.acct.adv"))}</button>` +
          `<button data-act="start" data-p="${p}" data-a="${id}">${this._esc(T("cp.acct.start"))}</button>` +
          `<button data-act="stop" data-p="${p}" data-a="${id}">${this._esc(T("cp.acct.stop"))}</button>`
        : "";
      const q = a.auto_reply_quota;
      let quotaTxt = "";
      if (q && a.auto_reply) {
        quotaTxt = q.circuit_open
          ? T("cp.acct.circuit_suffix")
          : T("cp.acct.today_suffix", { used: q.day_used, limit: q.day_limit || "∞" });
      }
      const disp = a.label || this._selfName(a) || this._maskId(a.account_id);
      const subId = a.self_username ? "@" + a.self_username : this._maskId(a.account_id);
      return `<div class="row" style="--row-accent:${this._esc(accent)}">${this._avatarHtml(a)}` +
        `<div class="meta"><div class="label">${this._esc(disp)}</div>` +
        `<div class="sub">${this._esc(a.platform)} · ${this._esc(subId)}${mode ? " · " + this._esc(mode) : ""}${this._esc(quotaTxt)}</div></div>` +
        `<span class="badge ${stCls}">${this._esc(stTxt)}</span>${ctrl}</div>`;
    }

    _onAction(act, el) {
      if (act === "refresh") return this.reload();
      if (act === "audit") return this._renderAudit();
      if (act === "health") return this._renderHealth();
      if (act === "webhooks") return this._renderWebhooks();
      if (act === "settings") return this._renderSettings();
      if (act === "wh-add") return this._whAddRow();
      if (act === "wh-del") return this._whDelRow(el);
      if (act === "wh-test") return this._whTest(el);
      if (act === "wh-save") return this._whSave(el);
      if (act === "save-config") return this._saveConfig(el);
      if (act === "add") return this._renderAddForm();
      if (act === "cancel-add") { this._stopPoll(); this._stopAuditLive(); return this.reload(); }
      if (act === "modes") return this._loadModes(el);
      if (act === "start-login") return this._startLogin();
      if (act === "start" || act === "stop") return this._accountCtrl(act, el);
      if (act === "toggle-auto") return this._toggleAuto(el);
      if (act === "adv") return this._renderOverride(el);
      if (act === "save-override") return this._saveOverride(el);
      if (act === "reset-override") return this._resetOverride(el);
    }

    _findAccount(platform, account_id) {
      return (this._accounts || []).find(
        (a) => a.platform === platform && a.account_id === account_id) || null;
    }

    _renderOverride(el) {
      this._stopPoll(); this._stopAuditLive();
      const platform = el.getAttribute("data-p");
      const account_id = el.getAttribute("data-a");
      const a = this._findAccount(platform, account_id) || {};
      const ov = a.auto_reply_override || {};
      const rate = ov.rate || {}, hrs = ov.hours || {}, dl = ov.delay || {};
      const form = this.shadowRoot.querySelector(".form");
      if (!form) return;
      const v = (x) => (x == null ? "" : x);
      const ck = (x) => (x ? "checked" : "");
      const gph = this._esc(T("cp.common.global"));
      form.innerHTML =
        `<div class="add" data-p="${this._esc(platform)}" data-a="${this._esc(account_id)}">` +
        `<div class="sub">${this._esc(T("cp.acct.ov_title", { label: a.label || account_id }))}</div>` +
        `<div class="field"><label>${this._esc(T("cp.acct.hourly"))}</label><input class="o-hourly" type="number" placeholder="${gph}" value="${this._esc(v(rate.hourly))}"/>` +
        `<label>${this._esc(T("cp.acct.daily"))}</label><input class="o-daily" type="number" placeholder="${gph}" value="${this._esc(v(rate.daily))}"/></div>` +
        `<div class="field"><label>${this._esc(T("cp.acct.hours"))}</label><input type="checkbox" class="o-hen" ${ck(hrs.enabled)} style="flex:0"/>` +
        `<input class="o-hst" placeholder="09:00" value="${this._esc(v(hrs.start))}" style="max-width:64px"/>–` +
        `<input class="o-hed" placeholder="23:00" value="${this._esc(v(hrs.end))}" style="max-width:64px"/></div>` +
        `<div class="field"><label>${this._esc(T("cp.acct.delay_sec"))}</label><input class="o-dmin" type="number" placeholder="${gph}" value="${this._esc(v(dl.min_sec))}"/>–` +
        `<input class="o-dmax" type="number" placeholder="${gph}" value="${this._esc(v(dl.max_sec))}"/></div>` +
        `<div class="field" style="justify-content:flex-end;gap:8px">` +
        `<button data-act="reset-override" class="danger">${this._esc(T("cp.acct.clear_ov"))}</button>` +
        `<button data-act="cancel-add">${this._esc(T("cp.common.cancel"))}</button>` +
        `<button class="primary" data-act="save-override">${this._esc(T("cp.common.save"))}</button></div>` +
        `<div class="o-msg sub"></div></div>`;
    }

    _collectOverride(form) {
      const q = (s) => form.querySelector(s);
      const intOr = (s) => { const t = q(s).value.trim(); if (t === "") return undefined; const n = parseInt(t, 10); return isNaN(n) ? undefined : n; };
      const ov = {};
      const rate = {};
      if (intOr(".o-hourly") !== undefined) rate.hourly = intOr(".o-hourly");
      if (intOr(".o-daily") !== undefined) rate.daily = intOr(".o-daily");
      if (Object.keys(rate).length) ov.rate = rate;
      const hours = {};
      if (q(".o-hen").checked) hours.enabled = true;
      if (q(".o-hst").value.trim()) hours.start = q(".o-hst").value.trim();
      if (q(".o-hed").value.trim()) hours.end = q(".o-hed").value.trim();
      if (Object.keys(hours).length) ov.hours = hours;
      const delay = {};
      if (intOr(".o-dmin") !== undefined) delay.min_sec = intOr(".o-dmin");
      if (intOr(".o-dmax") !== undefined) delay.max_sec = intOr(".o-dmax");
      if (Object.keys(delay).length) ov.delay = delay;
      return ov;
    }

    async _saveOverride(el) {
      const box = this.shadowRoot.querySelector(".form .add");
      if (!box || !this._client.setAccountOverride) return;
      const platform = box.getAttribute("data-p");
      const account_id = box.getAttribute("data-a");
      const override = this._collectOverride(box);
      el.disabled = true;
      const msg = box.querySelector(".o-msg");
      try {
        const d = await this._client.setAccountOverride({ platform, account_id, override });
        if (msg) {
          msg.textContent = (d && d.ok) ? T("cp.acct.saved_refresh") : T("cp.common.save_fail");
          msg.style.color = (d && d.ok) ? "#2f9e6e" : "var(--cp-danger,#dc2626)";
        }
        setTimeout(() => this.reload(), 700);
      } catch (e) {
        if (msg) { msg.textContent = T("cp.common.save_fail"); msg.style.color = "var(--cp-danger,#dc2626)"; }
        el.disabled = false;
      }
    }

    async _resetOverride(el) {
      const box = this.shadowRoot.querySelector(".form .add");
      if (!box || !this._client.setAccountOverride) return;
      const platform = box.getAttribute("data-p");
      const account_id = box.getAttribute("data-a");
      el.disabled = true;
      try {
        await this._client.setAccountOverride({ platform, account_id, override: { reset: true } });
      } catch (e) { /* ignore */ }
      setTimeout(() => this.reload(), 500);
    }

    async _toggleAuto(el) {
      if (!this._client || !this._client.setAutoReply) return;
      const platform = el.getAttribute("data-p");
      const account_id = el.getAttribute("data-a");
      const enabled = el.getAttribute("data-v") !== "1";
      el.disabled = true;
      try {
        await this._client.setAutoReply({ platform, account_id, enabled });
      } catch (e) { /* ignore */ }
      setTimeout(() => this.reload(), 400);
    }

    async _accountCtrl(act, el) {
      const platform = el.getAttribute("data-p");
      const account_id = el.getAttribute("data-a");
      el.disabled = true;
      try {
        if (act === "start") await this._client.accountStart({ platform, account_id });
        else await this._client.accountStop({ platform, account_id });
      } catch (e) { /* ignore */ }
      setTimeout(() => this.reload(), 600);
    }

    _auditRowHtml(it) {
      const HANDOFF = ["high_risk", "empty_reply", "generate_error", "send_error",
        "quota_hour", "quota_day", "circuit_open", "off_hours"];
      const sent = it.decision === "sent";
      const cls = sent ? "sent" : (HANDOFF.indexOf(it.reason) >= 0 ? "handoff" : "");
      const reasonKey = "cp.acct.reason." + (it.reason || "");
      const rt = it.reason ? (T(reasonKey) === reasonKey ? it.reason : T(reasonKey)) : "";
      const t = it.ts ? new Date(it.ts * 1000).toLocaleTimeString() : "";
      const inb = this._esc(String(it.inbound || "").slice(0, 80));
      const rep = this._esc(String(it.reply || "").slice(0, 120));
      return `<div class="it"><div class="hd"><span class="rb ${cls}">${this._esc(rt)}</span>` +
        `<span>${this._esc(it.platform)}·${this._esc(it.account_id)}</span>` +
        `<span style="flex:1"></span><span>${this._esc(t)}</span></div>` +
        `<div class="tx">${this._esc(T("cp.acct.cust_pfx"))}${inb}${rep ? "\n" + this._esc(T("cp.acct.ai_pfx")) + rep : ""}</div></div>`;
    }

    async _renderAudit() {
      this._stopPoll();
      const form = this.shadowRoot.querySelector(".form");
      if (!form || !this._client || !this._client.autoReplyAudit) return;
      form.innerHTML = `<div class="audit"><div class="sum">${this._esc(T("cp.common.loading"))}</div></div>`;
      let d;
      try {
        d = await this._client.autoReplyAudit({ limit: 50 });
      } catch (e) {
        form.innerHTML = `<div class="audit"><div class="err">${this._esc(T("cp.acct.audit_err"))}</div></div>`;
        return;
      }
      const items = (d && d.items) || [];
      const st = (d && d.stats) || {};
      const gOn = !!(d && d.global_enabled);
      const rows = items.map((it) => this._auditRowHtml(it)).join("") ||
        `<div class="empty">${this._esc(T("cp.acct.audit_empty"))}</div>`;
      const sum = T("cp.acct.audit_sum", {
        on: gOn ? T("cp.common.on") : T("cp.common.off"), sent: st.sent || 0, skipped: st.skipped || 0,
      });
      form.innerHTML = `<div class="audit"><div class="sum">${this._esc(sum)}` +
        `<span class="live" title="${this._esc(T("cp.acct.live_title"))}">● LIVE</span>` +
        `<button data-act="cancel-add">${this._esc(T("cp.common.close"))}</button></div>` +
        `<div class="audit-list">${rows}</div></div>`;
      this._startAuditLive();
    }

    _prependAuditItem(it) {
      const list = this.shadowRoot.querySelector(".audit-list");
      if (!list || !it) return;
      const empty = list.querySelector(".empty");
      if (empty) empty.remove();
      list.insertAdjacentHTML("afterbegin", this._auditRowHtml(it));
      while (list.children.length > 80) list.removeChild(list.lastChild);
    }

    _startAuditLive() {
      this._stopAuditLive();
      const onItem = (it) => this._prependAuditItem(it);
      // 优先 SSE（web 同源）；失败/不可用 → 回落轮询（桌面/鉴权受限场景）
      if (this._client.openAuditStream && typeof EventSource !== "undefined") {
        let fellBack = false;
        const fallback = () => {
          if (fellBack) return;
          fellBack = true; this._auditES = null; this._pollAudit();
        };
        try {
          this._auditES = this._client.openAuditStream(onItem, fallback);
          if (this._auditES) return;
        } catch (e) { /* 落轮询 */ }
      }
      this._pollAudit();
    }

    _pollAudit() {
      this._auditSeen = this._auditSeen || 0;
      this._auditPoll = setInterval(async () => {
        if (!this._client || !this.shadowRoot.querySelector(".audit-list")) {
          return this._stopAuditLive();
        }
        try {
          const d = await this._client.autoReplyAudit({ limit: 20 });
          const items = ((d && d.items) || []).slice().reverse(); // 旧→新
          for (const it of items) {
            if (Number(it.id) > this._auditSeen) {
              this._auditSeen = Number(it.id);
              this._prependAuditItem(it);
            }
          }
        } catch (e) { /* ignore */ }
      }, 3000);
    }

    _stopAuditLive() {
      if (this._auditPoll) { clearInterval(this._auditPoll); this._auditPoll = null; }
      if (this._auditES) { try { this._auditES.close(); } catch (e) { /* */ } this._auditES = null; }
      this._auditSeen = 0;
    }

    async _renderHealth() {
      this._stopPoll(); this._stopAuditLive();
      const form = this.shadowRoot.querySelector(".form");
      if (!form || !this._client || !this._client.autoReplyHealth) return;
      form.innerHTML = `<div class="audit"><div class="sum">${this._esc(T("cp.acct.health_ing"))}</div></div>`;
      let h;
      try {
        h = await this._client.autoReplyHealth();
      } catch (e) {
        form.innerHTML = `<div class="audit"><div class="err">${this._esc(T("cp.acct.health_err"))}</div></div>`;
        return;
      }
      if (!h || h.ok === false) {
        form.innerHTML = `<div class="audit"><div class="err">${this._esc(T("cp.acct.health_err"))}</div></div>`;
        return;
      }
      const acc = h.accounts || {}, lim = h.limits || {}, st = h.stats_24h || {};
      const okBadge = h.healthy
        ? `<span class="rb sent">${this._esc(T("cp.acct.healthy"))}</span>`
        : `<span class="rb handoff">${this._esc(T("cp.acct.warns", { n: (h.warnings || []).length }))}</span>`;
      const line = (k, v) => `<div class="hd"><span>${this._esc(k)}</span><span style="flex:1"></span><span>${this._esc(v)}</span></div>`;
      const warns = (h.warnings || []).map(
        (w) => `<div class="tx" style="color:var(--cp-danger,#dc2626)">⚠ ${this._esc(w)}</div>`).join("");
      const changes = (h.recent_changes || []).map((c) => {
        const t = c.ts ? new Date(c.ts * 1000).toLocaleString() : "";
        const tgt = c.scope === "global" ? T("cp.common.global")
          : `${this._esc(c.platform || "")}:${this._esc(c.account_id || "")}`;
        const ch = (c.changes || []).map(
          (x) => `${this._esc(x.key)}: ${this._esc(JSON.stringify(x.old))}→${this._esc(JSON.stringify(x.new))}`).join("; ");
        return `<div class="it"><div class="hd"><span class="rb">${this._esc(c.scope || "")}</span>` +
          `<span>${tgt}</span><span style="flex:1"></span><span>${this._esc(c.actor || "")} ${this._esc(t)}</span></div>` +
          `<div class="tx">${ch}</div></div>`;
      }).join("") || `<div class="empty">${this._esc(T("cp.acct.no_changes"))}</div>`;
      form.innerHTML =
        `<div class="audit"><div class="sum">${this._esc(T("cp.acct.health_title"))} ${okBadge}` +
        `<button data-act="cancel-add">${this._esc(T("cp.common.close"))}</button></div>` +
        `<div class="it">` +
        line(T("cp.acct.l_gate"), h.global_enabled ? T("cp.common.on") : T("cp.common.off")) +
        line("SkillManager", h.skill_manager_ready ? T("cp.acct.ready") : T("cp.acct.not_ready")) +
        line(T("cp.acct.l_webhook"), h.webhook_alert_configured ? T("cp.acct.configured") : T("cp.acct.not_configured")) +
        line(T("cp.acct.l_auto_on"), `${acc.auto_reply_on || 0}${T("cp.acct.protocol_count", { n: acc.protocol || 0 })}`) +
        line(T("cp.acct.l_circuit"), (h.circuit_open || []).length ? (h.circuit_open || []).join(", ") : T("cp.common.none")) +
        line(T("cp.acct.l_quota"), T("cp.acct.quota_fmt", { hourly: lim.hourly || "∞", daily: lim.daily || "∞" })) +
        line(T("cp.acct.l_24h"), T("cp.acct.sent_skipped", { sent: st.sent || 0, skipped: st.skipped || 0 })) +
        `</div>` +
        (warns ? `<div class="it">${warns}</div>` : "") +
        `<div class="sub" style="margin-top:8px">${this._esc(T("cp.acct.recent_changes"))}</div>${changes}</div>`;
    }

    async _renderWebhooks() {
      this._stopPoll(); this._stopAuditLive();
      const form = this.shadowRoot.querySelector(".form");
      if (!form || !this._client || !this._client.autoReplyWebhooks) return;
      form.innerHTML = `<div class="add"><div class="sub">${this._esc(T("cp.acct.wh_loading"))}</div></div>`;
      let list = [];
      try {
        const d = await this._client.autoReplyWebhooks();
        list = (d && d.webhooks) || [];
      } catch (e) { /* 空列表 */ }
      const rows = list.map((w, i) => this._whRowHtml(w, i)).join("");
      form.innerHTML =
        `<div class="add">` +
        `<div class="sub">${this._esc(T("cp.acct.wh_desc"))}</div>` +
        `<div class="wh-list">${rows || `<div class="empty">${this._esc(T("cp.acct.wh_empty"))}</div>`}</div>` +
        `<div class="field" style="justify-content:space-between;gap:8px">` +
        `<button data-act="wh-add">${this._esc(T("cp.acct.wh_add"))}</button>` +
        `<span style="flex:1"></span>` +
        `<button data-act="cancel-add">${this._esc(T("cp.common.cancel"))}</button>` +
        `<button class="primary" data-act="wh-save">${this._esc(T("cp.acct.wh_save_all"))}</button></div>` +
        `<div class="wh-msg sub"></div></div>`;
    }

    _whRowHtml(w, i) {
      w = w || {};
      const e = (s) => this._esc(s == null ? "" : s);
      const fmt = w.format || "telegram";
      const fopt = (v, label) => `<option value="${v}"${fmt === v ? " selected" : ""}>${label}</option>`;
      const evs = (w.events || ["autoreply_alert"]).join(",");
      const tokenPh = w.token_set ? T("cp.acct.token_set") : T("cp.acct.token_ph");
      return `<div class="wh-it" data-i="${i}" style="border:1px solid var(--cp-border,#eef2f7);border-radius:8px;padding:6px;margin-bottom:6px">` +
        `<div class="field"><input class="w-name" placeholder="${e(T("cp.acct.wh_name_ph"))}" value="${e(w.name)}" style="max-width:120px"/>` +
        `<select class="w-fmt">${fopt("telegram", "Telegram")}${fopt("whatsapp", "WhatsApp")}${fopt("messenger", "Messenger")}${fopt("json", T("cp.acct.wh_json"))}</select>` +
        `<label style="flex:0"><input type="checkbox" class="w-en" ${w.enabled === false ? "" : "checked"}/>${e(T("cp.common.enable"))}</label>` +
        `<span style="flex:1"></span><button data-act="wh-test">${e(T("cp.common.test"))}</button><button data-act="wh-del">${e(T("cp.common.delete"))}</button></div>` +
        `<div class="field"><input class="w-token" placeholder="${e(tokenPh)}" value="" style="flex:1"/></div>` +
        `<div class="field"><input class="w-target" placeholder="${e(T("cp.acct.wh_target_ph"))}" value="${e(w.target)}" style="flex:1"/></div>` +
        `<div class="field"><input class="w-url" placeholder="${e(T("cp.acct.wh_url_ph"))}" value="${e(w.url)}" style="flex:1"/></div>` +
        `<div class="field"><input class="w-events" placeholder="${e(T("cp.acct.wh_events_ph"))}" value="${e(evs)}" style="flex:1"/></div>` +
        `</div>`;
    }

    _whAddRow() {
      const list = this.shadowRoot.querySelector(".wh-list");
      if (!list) return;
      const empty = list.querySelector(".empty");
      if (empty) empty.remove();
      const i = list.querySelectorAll(".wh-it").length;
      const tmp = document.createElement("div");
      tmp.innerHTML = this._whRowHtml(
        { format: "telegram", enabled: true, events: ["autoreply_alert"] }, i);
      list.appendChild(tmp.firstChild);
    }

    _whDelRow(el) {
      const it = el.closest(".wh-it");
      if (it) it.remove();
    }

    _whCollect() {
      const list = this.shadowRoot.querySelector(".wh-list");
      if (!list) return [];
      return Array.from(list.querySelectorAll(".wh-it")).map((it) => {
        const g = (s) => { const n = it.querySelector(s); return n ? n.value.trim() : ""; };
        const events = g(".w-events").split(",").map((x) => x.trim()).filter(Boolean);
        return {
          name: g(".w-name") || "webhook",
          format: it.querySelector(".w-fmt").value,
          enabled: it.querySelector(".w-en").checked,
          token: g(".w-token"),
          target: g(".w-target"),
          url: g(".w-url"),
          events: events.length ? events : ["autoreply_alert"],
        };
      });
    }

    async _whSave(el) {
      const msg = this.shadowRoot.querySelector(".wh-msg");
      el.disabled = true;
      try {
        const d = await this._client.setAutoReplyWebhooks(this._whCollect());
        if (msg) {
          msg.textContent = (d && d.ok) ? T("cp.acct.wh_saved", { count: d.count }) : T("cp.common.save_fail");
          msg.style.color = (d && d.ok) ? "#2f9e6e" : "var(--cp-danger,#dc2626)";
        }
      } catch (e) {
        if (msg) { msg.textContent = T("cp.common.save_fail"); msg.style.color = "var(--cp-danger,#dc2626)"; }
      }
      el.disabled = false;
    }

    async _whTest(el) {
      const it = el.closest(".wh-it");
      const msg = this.shadowRoot.querySelector(".wh-msg");
      if (!it) return;
      const idx = Array.from(it.parentNode.children).indexOf(it);
      const g = (s) => { const n = it.querySelector(s); return n ? n.value.trim() : ""; };
      const events = g(".w-events").split(",").map((x) => x.trim()).filter(Boolean);
      const webhook = {
        name: g(".w-name") || "test", format: it.querySelector(".w-fmt").value,
        token: g(".w-token"), target: g(".w-target"), url: g(".w-url"),
        events: events.length ? events : ["autoreply_alert"],
      };
      el.disabled = true;
      if (msg) { msg.textContent = T("cp.acct.wh_testing"); msg.style.color = ""; }
      try {
        const d = await this._client.testAutoReplyWebhook({ webhook, index: idx });
        if (msg) {
          msg.textContent = (d && d.ok) ? T("cp.acct.wh_test_ok")
            : T("cp.acct.wh_test_fail", { err: (d && d.error) || T("cp.acct.wh_unreachable") });
          msg.style.color = (d && d.ok) ? "#2f9e6e" : "var(--cp-danger,#dc2626)";
        }
      } catch (e) {
        if (msg) { msg.textContent = T("cp.acct.wh_test_fail_simple"); msg.style.color = "var(--cp-danger,#dc2626)"; }
      }
      el.disabled = false;
    }

    async _renderSettings() {
      this._stopPoll(); this._stopAuditLive();
      const form = this.shadowRoot.querySelector(".form");
      if (!form || !this._client || !this._client.autoReplyConfig) return;
      form.innerHTML = `<div class="add"><div class="field">${this._esc(T("cp.common.loading"))}</div></div>`;
      let s = {};
      try {
        const d = await this._client.autoReplyConfig();
        s = (d && d.settings) || {};
      } catch (e) { /* 用默认 */ }
      const rate = s.rate || {}, brk = s.breaker || {}, hrs = s.hours || {}, dl = s.delay || {};
      const ck = (v) => (v ? "checked" : "");
      form.innerHTML =
        `<div class="add">` +
        `<div class="field"><label>${this._esc(T("cp.acct.s_master"))}</label><input type="checkbox" class="c-enabled" ${ck(s.enabled)} style="flex:0"/>` +
        `<span class="sub">${this._esc(T("cp.acct.s_master_hint"))}</span></div>` +
        `<div class="field"><label>${this._esc(T("cp.acct.hourly"))}</label><input class="c-hourly" type="number" value="${this._esc(rate.hourly != null ? rate.hourly : 30)}"/>` +
        `<label>${this._esc(T("cp.acct.daily"))}</label><input class="c-daily" type="number" value="${this._esc(rate.daily != null ? rate.daily : 200)}"/></div>` +
        `<div class="field"><label>${this._esc(T("cp.acct.s_breaker_th"))}</label><input class="c-bth" type="number" value="${this._esc(brk.threshold != null ? brk.threshold : 5)}"/>` +
        `<label>${this._esc(T("cp.acct.s_cooldown"))}</label><input class="c-bcd" type="number" value="${this._esc(brk.cooldown_sec != null ? brk.cooldown_sec : 300)}"/></div>` +
        `<div class="field"><label>${this._esc(T("cp.acct.hours"))}</label><input type="checkbox" class="c-hen" ${ck(hrs.enabled)} style="flex:0"/>` +
        `<input class="c-hst" value="${this._esc(hrs.start || "09:00")}" style="max-width:64px"/>–` +
        `<input class="c-hed" value="${this._esc(hrs.end || "23:00")}" style="max-width:64px"/>` +
        `<label style="flex:0 0 auto">${this._esc(T("cp.acct.s_tz"))}</label><input class="c-htz" type="number" value="${this._esc(hrs.tz_offset != null ? hrs.tz_offset : 8)}" style="max-width:54px"/></div>` +
        `<div class="field"><label>${this._esc(T("cp.acct.delay_sec"))}</label><input class="c-dmin" type="number" value="${this._esc(dl.min_sec != null ? dl.min_sec : 0)}"/>–` +
        `<input class="c-dmax" type="number" value="${this._esc(dl.max_sec != null ? dl.max_sec : 0)}"/></div>` +
        `<div class="field" style="justify-content:flex-end;gap:8px">` +
        `<button data-act="cancel-add">${this._esc(T("cp.common.cancel"))}</button>` +
        `<button class="primary" data-act="save-config">${this._esc(T("cp.common.save"))}</button></div>` +
        `<div class="c-msg sub"></div></div>`;
    }

    async _saveConfig(el) {
      const form = this.shadowRoot.querySelector(".form");
      if (!form) return;
      const q = (s) => form.querySelector(s);
      const num = (s, d) => { const v = parseInt(q(s).value, 10); return isNaN(v) ? d : v; };
      const patch = {
        enabled: q(".c-enabled").checked,
        rate: { hourly: num(".c-hourly", 30), daily: num(".c-daily", 200) },
        breaker: { threshold: num(".c-bth", 5), cooldown_sec: num(".c-bcd", 300) },
        hours: {
          enabled: q(".c-hen").checked, start: q(".c-hst").value,
          end: q(".c-hed").value, tz_offset: num(".c-htz", 8),
        },
        delay: { min_sec: num(".c-dmin", 0), max_sec: num(".c-dmax", 0) },
      };
      el.disabled = true;
      const msg = q(".c-msg");
      try {
        const d = await this._client.setAutoReplyConfig(patch);
        if (msg) {
          msg.textContent = (d && d.ok) ? T("cp.acct.saved") : T("cp.common.save_fail");
          msg.style.color = (d && d.ok) ? "#2f9e6e" : "var(--cp-danger,#dc2626)";
        }
      } catch (e) {
        if (msg) { msg.textContent = T("cp.common.save_fail"); msg.style.color = "var(--cp-danger,#dc2626)"; }
      }
      el.disabled = false;
    }

    _renderAddForm() {
      this._stopAuditLive();
      const opts = PLATFORMS.map((p) => `<option value="${p.id}">${p.icon} ${p.name}</option>`).join("");
      const form = this.shadowRoot.querySelector(".form");
      form.innerHTML =
        `<div class="add">` +
        `<div class="field"><label>${this._esc(T("cp.acct.f_platform"))}</label><select class="f-plat">${opts}</select></div>` +
        `<div class="field"><label>${this._esc(T("cp.acct.f_mode"))}</label><select class="f-mode"><option value="">${this._esc(T("cp.common.default"))}</option></select></div>` +
        `<div class="field"><label>${this._esc(T("cp.acct.f_label"))}</label><input class="f-label" placeholder="${this._esc(T("cp.acct.f_label_ph"))}" /></div>` +
        `<div class="field" style="justify-content:flex-end;gap:8px">` +
        `<button data-act="cancel-add">${this._esc(T("cp.common.cancel"))}</button>` +
        `<button class="primary" data-act="start-login">${this._esc(T("cp.acct.start_login"))}</button></div>` +
        `<div class="qr"></div></div>`;
      const platSel = form.querySelector(".f-plat");
      platSel.addEventListener("change", () => this._loadModes());
      this._loadModes();
    }

    async _loadModes() {
      const form = this.shadowRoot.querySelector(".form");
      if (!form) return;
      const platform = form.querySelector(".f-plat").value;
      const modeSel = form.querySelector(".f-mode");
      modeSel.innerHTML = `<option value="">${this._esc(T("cp.common.default"))}</option>`;
      try {
        const d = await this._client.getPlatformModes({ platform });
        (d && d.modes || []).forEach((m) => {
          const o = document.createElement("option");
          o.value = m.mode;
          o.textContent = (m.label || m.mode) + (m.recommended ? T("cp.acct.recommended") : "") + (m.available === false ? T("cp.acct.not_enabled") : "");
          if (m.available === false) o.disabled = true;
          modeSel.appendChild(o);
        });
      } catch (e) { /* 保留默认 */ }
    }

    async _startLogin() {
      const form = this.shadowRoot.querySelector(".form");
      if (!form) return;
      const platform = form.querySelector(".f-plat").value;
      const mode = form.querySelector(".f-mode").value;
      const label = form.querySelector(".f-label").value;
      const qr = form.querySelector(".qr");
      qr.innerHTML = `<div class="inst">${this._esc(T("cp.acct.qr_requesting"))}</div>`;
      let d;
      try {
        d = await this._client.startLogin({ platform, mode, label });
      } catch (e) {
        qr.innerHTML = `<div class="st fail">${this._esc(T("cp.acct.login_req_fail"))}</div>`;
        return;
      }
      if (!d || d.ok === false) {
        qr.innerHTML = `<div class="st fail">${this._esc((d && d.detail) || T("cp.acct.login_cannot"))}</div>`;
        return;
      }
      this._login = { platform, login_id: d.login_id };
      this._paintQr(qr, d);
      this._startPoll(qr);
    }

    _paintQr(qr, d) {
      const img = d.qr_image
        ? `<img src="${this._esc(d.qr_image)}" alt="QR" />`
        : (d.qr_url ? `<div class="inst">${this._esc(T("cp.acct.login_link", { url: d.qr_url }))}</div>` : "");
      const inst = d.instruction ? `<div class="inst">${this._esc(d.instruction)}</div>` : "";
      qr.innerHTML = `${img}${inst}<div class="st">${this._esc(T("cp.acct.qr_waiting"))}</div>`;
    }

    _startPoll(qr) {
      this._stopPoll();
      this._poll = setInterval(async () => {
        if (!this._login) return this._stopPoll();
        let d;
        try {
          d = await this._client.loginStatus(this._login);
        } catch (e) { return; }
        const st = (d && d.status) || "";
        // provider 可能在轮询里刷新 QR（如 protocol 令牌轮换）
        if ((d && d.qr_image) || (d && d.qr_url)) {
          const stEl = qr.querySelector(".st");
          if (stEl && st !== "authorized") this._paintQr(qr, d);
        }
        const stEl = qr.querySelector(".st");
        if (st === "authorized") {
          this._stopPoll();
          if (stEl) { stEl.textContent = T("cp.acct.login_ok"); stEl.className = "st ok"; }
          setTimeout(() => this.reload(), 1000);
        } else if (st === "failed" || st === "expired") {
          this._stopPoll();
          if (stEl) { stEl.textContent = st === "expired" ? T("cp.acct.qr_expired") : T("cp.acct.login_fail"); stEl.className = "st fail"; }
        }
      }, 2500);
    }

    _stopPoll() {
      if (this._poll) { clearInterval(this._poll); this._poll = null; }
      if (this._login && this._client) {
        try { this._client.cancelLogin(this._login); } catch (e) { /* ignore */ }
      }
      this._login = null;
    }
  }

  if (!customElements.get("cp-accounts")) customElements.define("cp-accounts", CpAccounts);
  root.CopilotShared = Object.assign(root.CopilotShared || {}, { CpAccounts });
})(typeof window !== "undefined" ? window : this);
