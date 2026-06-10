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

  const LANGS = [
    ["", "跟随人设/客户"], ["zh", "中文"], ["en", "English"], ["th", "ไทย"],
    ["vi", "Tiếng Việt"], ["id", "Bahasa Indonesia"], ["ja", "日本語"],
    ["ko", "한국어"], ["ru", "Русский"], ["es", "Español"], ["pt", "Português"],
  ];
  const TIER = { chat_binding: "会话绑定", account_profile: "账号人设", domain: "域默认", default: "兜底" };

  class CpDraft extends Base {
    constructor() {
      super();
      this._genToken = 0;
      this.shadowRoot.addEventListener("change", (e) => {
        const s = e.target.closest('select[data-role="lang"]');
        if (s) this._saveLang(s.value);
      });
    }
    emptyText() { return "选中会话后可生成回复草稿"; }
    styles() {
      return `
      .ctl { display:flex; gap:var(--cp-gap-sm,6px); align-items:center; }
      select { font:inherit; font-size:var(--cp-fs-sm,12px); padding:5px 8px;
               border:1px solid var(--cp-border,#e2e8f0); border-radius:var(--cp-radius-sm,6px);
               background:var(--cp-surface,#fff); color:var(--cp-text,#1e293b); }
      button.gen { flex:1; }
      button.primary { background:var(--cp-accent,#4f46e5); color:#fff; border-color:transparent; padding:5px 10px; }
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
      button.force { background:var(--cp-danger,#dc2626); color:#fff; border-color:transparent; }`;
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
      this._render(
        `<div class="ctl"><select data-role="lang">${opts}</select>` +
        `<button class="primary gen" data-act="gen">✨ 生成回复草稿</button></div>` +
        `<div class="slot"></div>`
      );
    }

    onAction(act, el) {
      if (act === "gen") { this._generate(); return; }
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

      let messages = [];
      try {
        const h = await this._client.getHistory({ conversationId: cid, limit: 30 });
        messages = (h && Array.isArray(h.messages) ? h.messages : [])
          .map((m) => ({ direction: m.direction, text: m.text }))
          .filter((m) => m.text);
      } catch (e) { messages = []; }
      if (token !== this._genToken) return;
      if (!messages.length) {
        if (slot) slot.innerHTML = '<div class="err">无对话上下文,无法生成</div>';
        return;
      }

      let r;
      try {
        r = await this._client.smartReply({ messages, platform, chat_key: chatKey, target_lang: lang });
      } catch (e) { r = null; }
      if (token !== this._genToken) return;
      if (!r || !r.ok || !r.reply) {
        if (slot) slot.innerHTML = `<div class="err">${this.esc((r && r.detail) || "生成失败")}</div>`;
        return;
      }
      this._draft = r;
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
