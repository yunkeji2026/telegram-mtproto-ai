"use strict";
/* 两端共享组件 · 工作链执行(<cp-chain-exec>)— 继承 CpPanelBase
   对等网页端 _loadChainExecutions:执行卡(链名/状态/倒计时/步骤点/最后结果)+ 取消(running)。
   取消后派发 cp-action-done 并刷新。
   用法:
     const el = document.createElement('cp-chain-exec');
     el.client = CopilotShared.createCopilotClient();
     el.context = { conversationId };
   client 需实现:getChainExecutions / cancelChainExecution */
(function (root) {
  const Base = root.CopilotShared && root.CopilotShared.CpPanelBase;
  if (!Base) { console.error("cp-chain-exec: CpPanelBase 未加载"); return; }

  class CpChainExec extends Base {
    emptyText() { return "选中会话后加载工作链执行…"; }
    emptyDataText() { return "暂无工作链执行"; }
    errText() { return "工作链执行加载失败"; }
    styles() {
      return `
      .exec { border:1px solid var(--cp-border,#e2e8f0); border-radius:var(--cp-radius-sm,6px);
              padding:5px 8px; margin-bottom:var(--cp-gap-xs,4px); background:var(--cp-surface,#fff); }
      .exec.failed { border-color:var(--cp-danger,#dc2626); }
      .exec .name { font-size:var(--cp-fs-sm,12px); font-weight:var(--cp-fw-bold,600); color:#0f766e; }
      .exec .name .st { font-weight:400; color:var(--cp-text-tiny,#94a3b8); }
      .exec .step { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); }
      .exec .last { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-dim,#64748b); margin-top:2px; }
      .dots { display:flex; gap:3px; margin:3px 0; }
      .dot { width:6px; height:6px; border-radius:99px; background:var(--cp-track,#e2e8f0); }
      .dot.done { background:var(--cp-ok,#0f9d75); }
      .dot.active { background:var(--cp-accent,#4f46e5); }`;
    }

    async fetchData(ctx) {
      return this._client.getChainExecutions({ conversationId: ctx.conversationId, limit: 8 });
    }

    renderData(d) {
      const esc = (s) => this.esc(s);
      const execs = Array.isArray(d.executions) ? d.executions : [];
      if (!execs.length) return `<div class="empty">${this.emptyDataText()}</div>`;
      return execs.map((ex) => {
        const cls = "exec" + (ex.status === "failed" ? " failed" : "");
        const dots = (ex.steps_preview || []).map((s) => {
          let dc = "dot"; if (s.done) dc += " done"; else if (s.active) dc += " active";
          return `<div class="${dc}"></div>`;
        }).join("");
        const cd = ex.countdown_sec > 0 ? ` · ⏱${esc(ex.countdown_sec)}s` : "";
        const last = (ex.last_result && ex.last_result.text)
          ? `<div class="last">${esc((ex.last_result.text || "").slice(0, 50))}</div>` : "";
        const cancel = ex.status === "running"
          ? `<div class="acts"><button data-act="cancel" data-id="${esc(ex.exec_id)}">取消</button></div>` : "";
        return `<div class="${cls}">` +
          `<div class="name">${esc(ex.chain_name || "")} <span class="st">${esc(ex.status_label || "")}${cd}</span></div>` +
          `<div class="step">步骤 ${esc(ex.current_step_display)}/${esc(ex.total_steps)}` +
          (ex.current_step_label ? ` · ${esc(ex.current_step_label)}` : "") + `</div>` +
          `<div class="dots">${dots}</div>` + last + cancel +
          `</div>`;
      }).join("");
    }

    async onAction(act, el) {
      if (act !== "cancel") return;
      const execId = el.getAttribute("data-id");
      if (!execId) return;
      if (typeof confirm === "function" && !confirm("确认取消此工作链？")) return;
      el.disabled = true;
      let ok = false;
      try {
        const r = await this._client.cancelChainExecution({ execId });
        ok = !!(r && r.ok);
      } catch (e) { ok = false; }
      this.emit("cp-action-done", { action_type: "chain_cancel", ok });
      this.refresh();
    }
  }

  if (!customElements.get("cp-chain-exec")) customElements.define("cp-chain-exec", CpChainExec);
})(typeof window !== "undefined" ? window : this);
