"use strict";
/* 两端共享组件 · AI 回复草稿(<cp-draft>)— 继承 CpPanelBase(重写 refresh,不自动取数)
   自取持久化线程(getHistory by conversation_id)→ 走 /api/desktop/smart-reply 产线
   (SkillManager→意图→策略→KB→人设),返回人设化草稿 + 可选译文。
   关键:不传 persona_id —— 服务端按 chat_key 解析后台人设绑定(cp-persona 钉的),
   两组件经后端单一事实源联动;返回 persona/persona_tier 让徽标说真话。
   "使用"→emit cp-fill(回填由宿主落地)。回复语言按会话本地记忆。
   用法:
     const el = document.createElement('cp-draft');
     el.client = CopilotShared.createCopilotClient();
     el.context = { conversationId, chatKey, platform };
   client 需实现:getHistory / smartReply */
(function (root) {
  const Base = root.CopilotShared && root.CopilotShared.CpPanelBase;
  if (!Base) { console.error("cp-draft: CpPanelBase 未加载"); return; }

  // 语种统一用中文名显示，与后台翻译栏保持同一套（顺序/数量一致）
  const LANGS = [
    ["", "跟随人设/客户"], ["zh", "中文"], ["en", "英语"], ["th", "泰语"],
    ["vi", "越南语"], ["id", "印尼语"], ["ja", "日语"],
    ["ko", "韩语"], ["ru", "俄语"], ["es", "西班牙语"], ["pt", "葡萄牙语"],
  ];
  const TIER = { chat_binding: "会话绑定", account_profile: "账号人设", domain: "域默认", default: "兜底" };

  class CpDraft extends Base {
    constructor() {
      super();
      this._genToken = 0;
      this._pickSeq = 0;
      this.shadowRoot.addEventListener("change", (e) => {
        const s = e.target.closest('select[data-role="lang"]');
        if (s) { this._saveLang(s.value); this.emit("cp-lang-changed", { lang: s.value }); }
        const c = e.target.closest('select[data-role="contrast"]');
        if (c) this._saveContrast(c.value);
        const p = e.target.closest('select[data-role="persona"]');
        if (p) this._updatePinState();
      });
    }
    emptyText() { return "选中会话后可生成回复草稿"; }
    styles() {
      return `
      .ctl { display:flex; flex-direction:column; gap:var(--cp-gap-sm,6px); align-items:stretch; }
      select { font:inherit; font-size:var(--cp-fs-sm,12px); padding:5px 8px; width:100%;
               border:1px solid var(--cp-border,#e2e8f0); border-radius:var(--cp-radius-sm,6px);
               background:var(--cp-surface,#fff); color:var(--cp-text,#1e293b); }
      button.gen { width:100%; white-space:nowrap; padding:6px 10px;
                   background:var(--cp-ok,#0f9d75); color:#fff; border-color:transparent; font-weight:600; }
      button.gen:hover { filter:brightness(1.05); }
      button.primary { background:var(--cp-accent,#4f46e5); color:#fff; border-color:transparent; padding:6px 10px; white-space:nowrap; }
      .slot { margin-top:var(--cp-gap-sm,6px); }
      .draft { background:var(--cp-surface-2,#f8fafc); border:1px solid var(--cp-border,#e2e8f0);
               border-radius:var(--cp-radius-sm,6px); padding:7px 9px; }
      .badges { display:flex; gap:var(--cp-gap-xs,4px); flex-wrap:wrap; margin-bottom:4px; }
      .bdg { font-size:var(--cp-fs-tiny,11px); padding:1px 7px; border-radius:99px;
             background:var(--cp-accent-weak,rgba(79,70,229,.1)); color:var(--cp-accent,#4f46e5); }
      .bdg.intent { background:var(--cp-surface,#eef2ff); color:var(--cp-text-dim,#64748b); }
      .reply { font-size:var(--cp-fs,13px); color:var(--cp-text,#1e293b); line-height:1.5; white-space:pre-wrap; }
      .tr { margin-top:5px; padding-top:5px; border-top:1px dashed var(--cp-border,#e2e8f0);
            font-size:var(--cp-fs-sm,12px); color:var(--cp-text-dim,#475569); white-space:pre-wrap; }
      .tr .tl { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8); }
      .slot .empty,.slot .err { font-size:var(--cp-fs-sm,12px); }
      .guardbox { margin-top:5px; }
      .guard { font-size:var(--cp-fs-tiny,11px); padding:4px 8px; border-radius:var(--cp-radius-sm,6px);
               display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
      .guard.high { background:rgba(220,38,38,.12); color:var(--cp-danger,#dc2626); }
      .guard.medium { background:rgba(217,119,6,.14); color:var(--cp-warn,#92400e); }
      .guard.low { background:rgba(15,157,117,.12); color:var(--cp-ok,#0f9d75); }
      button.send { background:var(--cp-ok,#0f9d75); color:#fff; border-color:transparent; }
      button.force { background:var(--cp-danger,#dc2626); color:#fff; border-color:transparent; }
      .prow { display:flex; align-items:center; gap:var(--cp-gap-sm,6px); margin-bottom:var(--cp-gap-sm,6px); }
      .prow label { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); flex:0 0 auto; }
      .prow select { flex:1; min-width:0; }
      button.pin { flex:0 0 auto; padding:5px 8px; }
      button.pin.on { background:var(--cp-accent,#4f46e5); color:#fff; border-color:transparent; }
      .psrc { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8); margin:-2px 0 6px; }
      .lblock { border:1px solid var(--cp-border,#e2e8f0); border-radius:var(--cp-radius-sm,6px); padding:6px 8px; margin-top:6px; }
      .lblock.active { border-color:var(--cp-accent,#4f46e5); box-shadow:0 0 0 1px var(--cp-accent,#4f46e5) inset; }
      .lblock .lhead { display:flex; align-items:center; gap:6px; font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); margin-bottom:4px; }
      .lblock textarea { width:100%; box-sizing:border-box; font:inherit; font-size:var(--cp-fs-sm,12px);
               border:1px solid var(--cp-border,#e2e8f0); border-radius:var(--cp-radius-sm,6px); padding:5px 7px;
               resize:vertical; color:var(--cp-text,#1e293b); background:var(--cp-surface,#fff); }`;
    }

    // 重写:不自动取数(避免每次切会话就烧 LLM),只渲染控件 + 空 slot
    async refresh() {
      if (!this._client || !this._ctx || !this._ctx.conversationId) {
        this._render(`<div class="empty">${this.esc(this.emptyText())}</div>`);
        return;
      }
      this._draft = null;
      const lang = this._loadLang();
      const opts = LANGS.map(([v, l]) =>
        `<option value="${v}"${v === lang ? " selected" : ""}>${this.esc(l)}</option>`).join("");
      const wantPersona = this.hasAttribute("persona");
      const wantContrast = this.hasAttribute("contrast");
      let personaRow = "";
      if (wantPersona) {
        personaRow =
          `<div class="prow"><label>人设</label>` +
          `<select data-role="persona"><option value="">默认（按后台绑定）</option></select>` +
          `<button class="pin" data-act="pin" title="把当前人设钉到后台（该会话全端生效，含 RPA 自动回复）">📌</button></div>` +
          `<div class="psrc" data-role="psrc"></div>`;
      }
      let contrastRow = "";
      if (wantContrast) {
        const cl = this._loadContrast();
        const copts = LANGS.filter(([v]) => v !== "")
          .map(([v, l]) => `<option value="${v}"${v === cl ? " selected" : ""}>${this.esc(l)}</option>`).join("");
        contrastRow = `<select data-role="contrast"><option value="">不对比</option>${copts}</select>`;
      }
      this._render(
        personaRow +
        `<div class="ctl"><select data-role="lang">${opts}</select>` +
        contrastRow +
        `<button class="gen" data-act="gen">生成草稿</button></div>` +
        `<div class="slot"></div>`
      );
      if (wantPersona) this._loadPersonas();
    }

    _contrastKey() { return "cp_contrastlang:" + (this._ctx && this._ctx.conversationId || ""); }
    _loadContrast() { try { return localStorage.getItem(this._contrastKey()) || ""; } catch (e) { return ""; } }
    _saveContrast(v) { try { if (v) localStorage.setItem(this._contrastKey(), v); else localStorage.removeItem(this._contrastKey()); } catch (e) {} }
    _langLabel(code) { const f = LANGS.find(([v]) => v === code); return f ? f[1] : code; }

    async _loadPersonas() {
      const sel = this.shadowRoot.querySelector('select[data-role="persona"]');
      if (!sel || !this._client) return;
      let summary = [];
      this._profiles = {};
      try {
        const d = await this._client.listPersonas();
        summary = (d && d.summary) || [];
        this._profiles = (d && d.profiles) || {};
      } catch (e) {}
      let bound = "";
      try {
        const b = await this._client.getPersonaBindings();
        const ck = (this._ctx && this._ctx.chatKey) || "";
        const bd = (b && b.bindings && ck) ? b.bindings[ck] : null;
        bound = bd ? (bd.id || "") : "";
      } catch (e) {}
      let h = '<option value="">默认（按后台绑定）</option>';
      summary.forEach((p) => {
        const id = (p && p.id) || "";
        const nm = p && p.role ? `${p.name}（${p.role}）` : ((p && (p.name || p.id)) || id);
        h += `<option value="${this.esc(id)}"${id === bound ? " selected" : ""}>${this.esc(nm)}</option>`;
      });
      sel.innerHTML = h;
      this._updatePinState();
    }

    _updatePinState() {
      const sel = this.shadowRoot.querySelector('select[data-role="persona"]');
      const pin = this.shadowRoot.querySelector("button.pin");
      const src = this.shadowRoot.querySelector('[data-role="psrc"]');
      if (pin && sel) pin.classList.toggle("on", !!sel.value);
      if (src && sel) src.textContent = sel.value ? "已钉到后台 · 全端（含 RPA）生效" : "未钉绑 · 跟随账号/域默认";
    }

    async _pinPersona() {
      const sel = this.shadowRoot.querySelector('select[data-role="persona"]');
      const pid = sel ? sel.value : "";
      const chatKey = (this._ctx && this._ctx.chatKey) || "";
      if (!chatKey || !this._client) return;
      let ok = false;
      try {
        if (pid) {
          const persona = (this._profiles || {})[pid];
          if (!persona) return;
          const r = await this._client.bindPersona({ chatKey, persona });
          ok = !!(r && r.ok);
        } else {
          const r = await this._client.unbindPersona({ chatKey });
          ok = !!(r && r.ok);
        }
      } catch (e) { ok = false; }
      this._updatePinState();
      this.emit("cp-persona-pinned", { personaId: pid, chatKey, ok });
    }

    onAction(act, el) {
      if (act === "gen") { this._generate(); return; }
      if (act === "pin") { this._pinPersona(); return; }
      if (act === "fill-pick") {
        const t = this._pickedText();
        if (t) this.emit("cp-fill", { text: t, source: "draft" });
        return;
      }
      if (act === "send-pick") {
        const t = this._pickedText();
        if (t) this._guardThenSend(t, "reply");
        return;
      }
      const d = this._draft || {};
      const textOf = (which) => (which === "translated" ? (d.translated || "") : (d.reply || ""));
      if (act === "fill") {
        const text = textOf(el.getAttribute("data-which"));
        if (text) this.emit("cp-fill", { text, source: "draft" });
        return;
      }
      if (act === "send") {
        const which = el.getAttribute("data-which") || "reply";
        const text = textOf(which);
        if (text) this._guardThenSend(text, which);
        return;
      }
      if (act === "send-force") {
        const which = el.getAttribute("data-which") || "reply";
        const text = textOf(which);
        if (text) this._emitSend(text, which, "high");
      }
    }

    async _guardThenSend(text, which) {
      const box = this.shadowRoot.querySelector(".guardbox");
      let g;
      try { g = await this._client.guardCheck({ text }); } catch (e) { g = null; }
      if (!g || !g.ok) { this._emitSend(text, which, "low"); return; } // 护栏不可用不阻断发送
      if (box) box.innerHTML = this._guardBanner(g, which);
      if (g.block) return; // 高风险:等待二次确认(force)
      this._emitSend(text, which, g.risk || "low");
    }

    _guardBanner(g, which) {
      const esc = (s) => this.esc(s);
      const cls = g.risk === "high" ? "high" : g.risk === "medium" ? "medium" : "low";
      const msg = g.risk === "high" ? "⛔ 高风险（支付/密码/账号安全）"
        : g.risk === "medium" ? "⚠ 中风险，请人工确认" : "✓ 未见敏感词";
      const hits = (g.hits || []).map((h) => esc(h.term)).join("、");
      const rob = (g.robotic || []).length ? " · 机器措辞：" + (g.robotic || []).map(esc).join("、") : "";
      const force = g.block
        ? `<button class="force" data-act="send-force" data-which="${esc(which)}">确认无误，仍要发送</button>` : "";
      return `<div class="guard ${cls}">${msg}${hits ? " · 命中：" + hits : ""}${rob}${force}</div>`;
    }

    _emitSend(text, which, risk) {
      const box = this.shadowRoot.querySelector(".guardbox");
      if (box) box.innerHTML = "";
      this.emit("cp-send", { text, which, risk, conversationId: this._ctx && this._ctx.conversationId });
    }

    _pickedText() {
      const root = this.shadowRoot;
      const picked = root.querySelector("input[data-pick]:checked");
      const which = picked ? picked.getAttribute("data-pick") : "reply";
      const ta = root.querySelector(which === "contrast" ? '[data-role="contrast-ta"]' : '[data-role="reply-ta"]');
      return ta ? ta.value : "";
    }

    async _translateInto(ta, text, lang) {
      const s = (text || "").trim();
      if (!ta) return;
      if (!s) { ta.value = ""; ta.disabled = false; return; }
      ta.disabled = true; ta.value = "翻译中…";
      let r;
      try { r = await this._client.translate({ text: s, target_lang: lang }); } catch (e) { r = null; }
      ta.value = (r && r.ok && r.text) ? r.text : "翻译失败";
      ta.disabled = false;
    }

    async _wireContrast(replyText, contrastLang) {
      const root = this.shadowRoot;
      const replyTa = root.querySelector('[data-role="reply-ta"]');
      const contrastTa = root.querySelector('[data-role="contrast-ta"]');
      const blocks = root.querySelectorAll(".lblock");
      root.querySelectorAll("input[data-pick]").forEach((rb) => {
        rb.addEventListener("change", () => {
          blocks.forEach((b) => b.classList.toggle("active", b.getAttribute("data-block") === rb.getAttribute("data-pick") && rb.checked));
        });
      });
      if (replyTa) replyTa.addEventListener("focus", () => { const r = root.querySelector('input[data-pick="reply"]'); if (r) { r.checked = true; r.dispatchEvent(new Event("change", { bubbles: true })); } });
      if (contrastTa) contrastTa.addEventListener("focus", () => { const r = root.querySelector('input[data-pick="contrast"]'); if (r) { r.checked = true; r.dispatchEvent(new Event("change", { bubbles: true })); } });
      await this._translateInto(contrastTa, replyText, contrastLang);
      let userEdited = false;
      if (contrastTa) contrastTa.addEventListener("input", () => { userEdited = true; });
      let t;
      if (replyTa) replyTa.addEventListener("input", () => {
        if (userEdited) return;
        clearTimeout(t);
        t = setTimeout(() => this._translateInto(contrastTa, replyTa.value, contrastLang), 600);
      });
    }

    _slot() { return this.shadowRoot.querySelector(".slot"); }
    _langKey() { return "cp_replylang:" + (this._ctx && this._ctx.conversationId || ""); }
    _loadLang() { try { return localStorage.getItem(this._langKey()) || ""; } catch (e) { return ""; } }
    _saveLang(v) { try { if (v) localStorage.setItem(this._langKey(), v); else localStorage.removeItem(this._langKey()); } catch (e) {} }

    async _generate() {
      const ctx = this._ctx;
      if (!this._client || !ctx || !ctx.conversationId) return;
      const cid = ctx.conversationId;
      const parts = String(cid).split(":");
      const platform = ctx.platform || parts[0] || "telegram";
      const chatKey = ctx.chatKey || (parts.length >= 3 ? parts.slice(2).join(":") : "");
      const sel = this.shadowRoot.querySelector('select[data-role="lang"]');
      const lang = sel ? sel.value : "";
      const slot = this._slot();
      if (slot) slot.innerHTML = '<div class="empty">生成中…</div>';
      const token = ++this._genToken;

      // 上下文来源:宿主可注入 messagesProvider(桌面=webview 实时消息,未必落后端 inbox);
      // 不设则自取持久化线程(后台=getHistory by conversation_id)。
      let raw = [];
      try {
        if (typeof this._messagesProvider === "function") {
          raw = (await this._messagesProvider({ conversationId: cid, platform, chatKey })) || [];
        } else {
          const h = await this._client.getHistory({ conversationId: cid, limit: 30 });
          raw = h && Array.isArray(h.messages) ? h.messages : [];
        }
      } catch (e) { raw = []; }
      const messages = (Array.isArray(raw) ? raw : [])
        .map((m) => ({ direction: m.direction, text: m.text }))
        .filter((m) => m.text);
      if (token !== this._genToken) return;
      if (!messages.length) {
        if (slot) slot.innerHTML = '<div class="err">无对话上下文,无法生成</div>';
        return;
      }

      const personaSel = this.shadowRoot.querySelector('select[data-role="persona"]');
      const personaId = personaSel ? personaSel.value : "";
      const payload = { messages, platform, chat_key: chatKey, target_lang: lang };
      if (personaId) payload.persona_id = personaId;
      let r;
      try {
        r = await this._client.smartReply(payload);
      } catch (e) { r = null; }
      if (token !== this._genToken) return;
      if (!r || !r.ok || !r.reply) {
        if (slot) slot.innerHTML = `<div class="err">${this.esc((r && r.detail) || "生成失败")}</div>`;
        return;
      }
      this._draft = r;
      this._draftLang = lang || "zh";
      this._paintDraft(r);
    }

    _paintDraft(r) {
      const esc = (s) => this.esc(s);
      const slot = this._slot();
      if (!slot) return;
      const tierLbl = TIER[r.persona_tier] || r.persona_tier || "";
      const badges =
        (r.persona ? `<span class="bdg">🎭 ${esc(r.persona)}${tierLbl ? " · " + esc(tierLbl) : ""}</span>` : "") +
        (r.intent ? `<span class="bdg intent">意图 ${esc(r.intent)}</span>` : "");
      // —— 对比语言路径(桌面：reply/contrast 双块可编辑 + send-pick) ——
      const wantContrast = this.hasAttribute("contrast");
      const contrastSel = this.shadowRoot.querySelector('select[data-role="contrast"]');
      const contrastLang = contrastSel ? contrastSel.value : "";
      const replyLang = this._draftLang || "zh";
      // 指定回复语言时后端已用该语言生成(translated≈reply)，取可读文本
      const replyText = (this._loadLang() && r.translated) ? r.translated : r.reply;
      if (wantContrast && contrastLang && contrastLang !== replyLang) {
        this._pickSeq += 1;
        const nm = "cppick" + this._pickSeq;
        slot.innerHTML =
          `<div class="draft">` +
          (badges ? `<div class="badges">${badges}</div>` : "") +
          `<div class="lblock active" data-block="reply">` +
            `<div class="lhead"><input type="radio" name="${nm}" data-pick="reply" checked><span>${esc(this._langLabel(replyLang))}</span></div>` +
            `<textarea data-role="reply-ta" rows="4">${esc(replyText)}</textarea></div>` +
          `<div class="lblock" data-block="contrast">` +
            `<div class="lhead"><input type="radio" name="${nm}" data-pick="contrast"><span>${esc(this._langLabel(contrastLang))}</span></div>` +
            `<textarea data-role="contrast-ta" rows="4">翻译中…</textarea></div>` +
          `<div class="acts">` +
            `<button class="primary" data-act="fill-pick">填入输入框</button>` +
            `<button class="send" data-act="send-pick">填入并发送</button>` +
          `</div><div class="guardbox"></div></div>`;
        this._wireContrast(replyText, contrastLang);
        return;
      }
      // —— 默认路径(后台/无对比)：保持与原行为一致 ——
      const translated = r.translated
        ? `<div class="tr"><div class="tl">译文</div>${esc(r.translated)}` +
          `<div class="acts"><button data-act="fill" data-which="translated">填入译文</button></div></div>` : "";
      // 发送默认用客户可读文本:有译文发译文,否则发原文
      const sendWhich = r.translated ? "translated" : "reply";
      slot.innerHTML =
        `<div class="draft">` +
        (badges ? `<div class="badges">${badges}</div>` : "") +
        `<div class="reply">${esc(r.reply)}</div>` +
        `<div class="acts">` +
        `<button class="primary" data-act="fill" data-which="reply">填入输入框</button>` +
        `<button class="send" data-act="send" data-which="${sendWhich}">填入并发送</button>` +
        `</div>` +
        translated +
        `<div class="guardbox"></div>` +
        `</div>`;
    }
  }

  if (!customElements.get("cp-draft")) customElements.define("cp-draft", CpDraft);
})(typeof window !== "undefined" ? window : this);
