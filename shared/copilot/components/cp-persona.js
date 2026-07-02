"use strict";
/* 两端共享组件 · 后台人设绑定(<cp-persona>)— 继承 CpPanelBase
   只承载"权威的后台钉绑":列人设 + 读当前会话绑定 + 绑定/解绑(写 bindings_runtime.yaml,
   全端含 RPA 生效)。localStorage 记忆 / webview 注入等宿主 UX 不进组件,
   组件只在变更后派发 cp-persona-changed 事件供宿主联动。
   用法:
     const el = document.createElement('cp-persona');
     el.client = CopilotShared.createCopilotClient();
     el.context = { conversationId, chatKey };   // 绑定按 chatKey
   client 需实现:listPersonas / getPersonaBindings / bindPersona / unbindPersona */
(function (root) {
  const Base = root.CopilotShared && root.CopilotShared.CpPanelBase;
  if (!Base) { console.error("cp-persona: CpPanelBase 未加载"); return; }

  class CpPersona extends Base {
    constructor() {
      super();
      // select 变更非点击,基类只委托 click,这里补 change 委托(shadowRoot 稳定,跨重渲染保留)
      this.shadowRoot.addEventListener("change", (e) => {
        const s = e.target.closest('select[data-role="persona"]');
        if (s && !s.disabled) this._onSelect(s.value);
      });
    }
    emptyText() { return this.t("cp.persona.empty"); }
    emptyDataText() { return this.t("cp.persona.no_data"); }
    errText() { return this.t("cp.persona.err"); }
    styles() {
      return `
      .lbl { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); margin-bottom:var(--cp-gap-xs,4px); }
      select { width:100%; font:inherit; font-size:var(--cp-fs-sm,12px); padding:5px 8px;
               border:1px solid var(--cp-border,#e2e8f0); border-radius:var(--cp-radius-sm,6px);
               background:var(--cp-surface,#fff); color:var(--cp-text,#1e293b); }
      .src { font-size:var(--cp-fs-tiny,11px); margin-top:var(--cp-gap-xs,4px); }
      .src.bound { color:var(--cp-ok,#0f9d75); }
      .src.unbound { color:var(--cp-text-tiny,#94a3b8); }`;
    }

    async fetchData(ctx) {
      const [list, binds] = await Promise.all([
        this._client.listPersonas(),
        this._client.getPersonaBindings(),
      ]);
      if (list && list.ok === false) return list;
      const summary = (list && list.summary) || [];
      const profiles = (list && list.profiles) || {};
      const bindings = (binds && binds.bindings) || {};
      const bound = (ctx.chatKey && bindings[ctx.chatKey]) || null;
      return {
        ok: true, summary, profiles,
        boundId: bound ? (bound.id || "") : "",
        boundName: bound ? (bound.name || "") : "",
      };
    }

    renderData(d) {
      const esc = (s) => this.esc(s);
      const summary = Array.isArray(d.summary) ? d.summary : [];
      const boundId = d.boundId || "";
      const opts = [`<option value="">${esc(this.t("cp.persona.unbound_opt"))}</option>`]
        .concat(summary.map((p) => {
          const label = p.role ? `${p.name} (${p.role})` : (p.name || p.id);
          const sel = p.id === boundId ? " selected" : "";
          return `<option value="${esc(p.id)}"${sel}>${esc(label)}</option>`;
        }));
      const src = boundId
        ? `<div class="src bound">${esc(this.t("cp.persona.bound", { name: d.boundName || boundId }))}</div>`
        : `<div class="src unbound">${esc(this.t("cp.persona.unbound"))}</div>`;
      return `<div class="lbl">${esc(this.t("cp.persona.label"))}</div>` +
        `<select data-role="persona">${opts.join("")}</select>` + src;
    }

    async _onSelect(pid) {
      const chatKey = this._ctx && this._ctx.chatKey;
      if (!chatKey) return;
      const sel = this.shadowRoot.querySelector('select[data-role="persona"]');
      if (sel) sel.disabled = true;
      let ok = false;
      try {
        if (pid) {
          const persona = (this._d && this._d.profiles || {})[pid];
          if (!persona) { if (sel) sel.disabled = false; return; }
          const r = await this._client.bindPersona({ chatKey, persona });
          ok = !!(r && r.ok);
        } else {
          const r = await this._client.unbindPersona({ chatKey });
          ok = !!(r && r.ok);
        }
      } catch (e) { ok = false; }
      this.emit("cp-persona-changed", { personaId: pid, chatKey, ok });
      this.refresh();
    }
  }

  if (!customElements.get("cp-persona")) customElements.define("cp-persona", CpPersona);
})(typeof window !== "undefined" ? window : this);
