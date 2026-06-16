"use strict";
/* 两端共享组件 · 剧本话题建议(<cp-script>)
   对等网页端 _loadScriptTopics:展示当前阶段 + 话题卡片(使用→emit cp-fill;
   工作链→client.startChain)。
   用法:
     const el = document.createElement('cp-script');
     el.client = CopilotShared.createCopilotClient();
     el.context = { conversationId };
   client 需实现:getScriptTopics / startChain */
(function (root) {
  const Base = root.CopilotShared && root.CopilotShared.CpPanelBase;
  if (!Base) { console.error("cp-script: CpPanelBase 未加载"); return; }

  class CpScript extends Base {
    emptyText() { return "选中会话后加载话题建议…"; }
    emptyDataText() { return "暂无话题建议"; }
    styles() {
      return `
      .card { border-left-color:var(--cp-accent,#7c8cff); background:var(--cp-accent-weak,rgba(124,140,255,.16)); }
      .card .title { color:var(--cp-accent,#7c8cff); }`;
    }
    async fetchData(ctx) {
      return this._client.getScriptTopics({ conversationId: ctx.conversationId });
    }
    renderData(d) {
      const topics = Array.isArray(d.topics) ? d.topics : [];
      const esc = (s) => this.esc(s);
      const badge = (d.stage_label || d.stage)
        ? `<div class="stage-badge">当前阶段：${esc(d.stage_label || d.stage)}` +
          (d.next_stage_label ? ` → 下一阶 ${esc(d.next_stage_label)}` : "") + `</div>`
        : "";
      if (!topics.length) return badge + `<div class="empty">${this.emptyDataText()}</div>`;
      return badge + topics.map((t, i) => {
        const btns = [`<button data-act="fill" data-idx="${i}">使用</button>`];
        if (t.chain_id) btns.push(`<button data-act="chain" data-idx="${i}">⚡ 工作链</button>`);
        return `<div class="card">` +
          `<div class="title">${esc(t.title || "")}</div>` +
          (t.hint ? `<div class="hint">${esc(t.hint)}</div>` : "") +
          `<div class="body">${esc(t.opener || "")}</div>` +
          `<div class="acts">${btns.join("")}</div>` +
          `</div>`;
      }).join("");
    }
    onAction(act, el) {
      const idx = parseInt(el.getAttribute("data-idx"), 10);
      const t = (this._d && this._d.topics || [])[idx];
      if (!t) return;
      if (act === "fill") {
        if (t.opener) this.emit("cp-fill", { text: t.opener, source: "script" });
        return;
      }
      if (act === "chain") this._startChain(t.chain_id);
    }
    async _startChain(chainId) {
      const cid = this._ctx && this._ctx.conversationId;
      if (!cid || !chainId) return;
      let ok = false;
      try {
        const r = await this._client.startChain({ conversationId: cid, chainId });
        ok = !!(r && r.ok);
      } catch (e) { ok = false; }
      this.emit("cp-action-done", { action_type: "chain", ok, conversationId: cid });
    }
  }

  if (!customElements.get("cp-script")) customElements.define("cp-script", CpScript);
})(typeof window !== "undefined" ? window : this);
