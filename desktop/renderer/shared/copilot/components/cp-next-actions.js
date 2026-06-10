"use strict";
/* 两端共享组件 · 下一步最佳行动 NBA(<cp-next-actions>)
   对等网页端 _loadNextActions:展示推荐动作,template→回填话术(emit cp-fill),
   task/tag/escalate/note/chain→client.executeAction。完成后 emit cp-action-done 并刷新。
   用法:
     const el = document.createElement('cp-next-actions');
     el.client = CopilotShared.createCopilotClient();
     el.context = { conversationId };
   client 需实现:getNextActions / executeAction */
(function (root) {
  const Base = root.CopilotShared && root.CopilotShared.CpPanelBase;
  if (!Base) { console.error("cp-next-actions: CpPanelBase 未加载"); return; }

  const ACCENT = {
    escalate: { bg: "rgba(220,38,38,.10)", border: "var(--cp-danger,#dc2626)" },
    template: { bg: "rgba(13,148,136,.10)", border: "#0d9488" },
  };

  class CpNextActions extends Base {
    emptyText() { return "选中会话后加载建议操作…"; }
    emptyDataText() { return "暂无推荐"; }
    styles() {
      return `
      .card.escalate { border-left-color:var(--cp-danger,#dc2626); }
      .card.template { border-left-color:#0d9488; }`;
    }
    async fetchData(ctx) {
      return this._client.getNextActions({ conversationId: ctx.conversationId });
    }
    renderData(d) {
      const acts = Array.isArray(d.actions) ? d.actions : [];
      if (!acts.length) return `<div class="empty">${this.emptyDataText()}</div>`;
      const esc = (s) => this.esc(s);
      return acts.map((a, i) => {
        const t = a.action_type;
        const cls = t === "escalate" ? "card escalate" : t === "template" ? "card template" : "card";
        const btns = [];
        if (t === "template" && a.config && a.config.template_text) {
          btns.push(`<button data-act="fill" data-idx="${i}">📤 使用话术</button>`);
        }
        if (t === "task") btns.push(`<button data-act="exec" data-idx="${i}" data-kind="task">📅 创建任务</button>`);
        if (t === "tag" && a.config && (a.config.tag || a.config.tag_options)) {
          const opts = (a.config.tag_options || [a.config.tag]).filter(Boolean);
          opts.forEach((tag) => {
            btns.push(`<button data-act="exec" data-idx="${i}" data-kind="tag" data-tag="${esc(tag)}">🏷 ${esc(tag)}</button>`);
          });
        }
        if (t === "escalate") btns.push(`<button class="danger" data-act="exec" data-idx="${i}" data-kind="escalate">🔴 立即升级</button>`);
        if (t === "note") btns.push(`<button data-act="exec" data-idx="${i}" data-kind="note">📝 添加备注</button>`);
        if (t === "chain") btns.push(`<button data-act="exec" data-idx="${i}" data-kind="chain">⚡ 启动工作链</button>`);
        return `<div class="${cls}">` +
          `<div class="title">${esc(a.icon || "💡")} ${esc(a.name || "")}</div>` +
          (a.reason ? `<div class="reason">${esc(a.reason)}</div>` : "") +
          (btns.length ? `<div class="acts">${btns.join("")}</div>` : "") +
          `</div>`;
      }).join("");
    }
    onAction(act, el) {
      const idx = parseInt(el.getAttribute("data-idx"), 10);
      const a = (this._d && this._d.actions || [])[idx];
      if (!a) return;
      if (act === "fill") {
        const text = (a.config && a.config.template_text) || "";
        if (text) this.emit("cp-fill", { text, source: "nba" });
        return;
      }
      if (act === "exec") {
        const kind = el.getAttribute("data-kind") || a.action_type;
        let config = Object.assign({}, a.config || {});
        if (kind === "tag") config = { tag: el.getAttribute("data-tag") || config.tag };
        if (kind === "note") {
          const body = (typeof prompt === "function") ? prompt("输入内部备注内容：") : "";
          if (!body || !body.trim()) return;
          config = { note_body: body.trim() };
        }
        this._exec(a.action_id || "", kind, config);
      }
    }
    async _exec(action_id, action_type, config) {
      const cid = this._ctx && this._ctx.conversationId;
      if (!cid) return;
      this.shadowRoot.querySelectorAll("[data-act]").forEach((b) => (b.disabled = true));
      let ok = false;
      try {
        const r = await this._client.executeAction({ conversationId: cid, action_id, action_type, config });
        ok = !!(r && r.ok);
      } catch (e) { ok = false; }
      this.emit("cp-action-done", { action_type, ok, conversationId: cid });
      this.refresh();
    }
  }

  if (!customElements.get("cp-next-actions")) customElements.define("cp-next-actions", CpNextActions);
})(typeof window !== "undefined" ? window : this);
