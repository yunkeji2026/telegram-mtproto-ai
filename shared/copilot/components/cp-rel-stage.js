"use strict";
/* 两端共享组件 · 关系阶段(<cp-rel-stage>)— 继承 CpPanelBase
   与网页端 _loadRelationshipStage 功能对等:全字段展示 + 确认进阶/确认回暖/
   手动降级/客户级对齐。动作完成后刷新并派发 cp-rel-changed 事件供宿主联动。
   用法:
     const el = document.createElement('cp-rel-stage');
     el.client = CopilotShared.createCopilotClient();
     el.context = { conversationId };
   client 需实现:getRelStage / confirmStage / downgradeStage / reunionStage / syncContactStage */
(function (root) {
  const Base = root.CopilotShared && root.CopilotShared.CpPanelBase;
  if (!Base) { console.error("cp-rel-stage: CpPanelBase 未加载"); return; }

  class CpRelStage extends Base {
    emptyText() { return this.t("cp.rel.empty"); }
    emptyDataText() { return this.t("cp.rel.no_data"); }
    errText() { return this.t("cp.rel.err"); }
    styles() {
      return `
      .stage { font-weight:var(--cp-fw-bold,700); font-size:var(--cp-fs-lg,15px); color:var(--cp-accent,#4f46e5); }
      .track { height:6px; border-radius:99px; background:var(--cp-track,#e2e8f0);
               margin:var(--cp-gap-sm,6px) 0; overflow:hidden; }
      .bar { height:100%; border-radius:99px; background:var(--cp-accent,#4f46e5);
             width:0; transition:width var(--cp-dur,.2s) ease; }
      .steps { display:flex; gap:var(--cp-gap-xs,4px); margin:var(--cp-gap-sm,6px) 0; }
      .step { flex:1; height:5px; border-radius:99px; background:var(--cp-track,#e2e8f0); }
      .step.done { background:var(--cp-ok,#0f9d75); }
      .step.active { background:var(--cp-accent,#4f46e5); }
      .step.pending { background:var(--cp-warn,#d97706); }
      .meta { display:flex; gap:var(--cp-gap,10px); flex-wrap:wrap;
              font-size:var(--cp-fs-sm,12px); color:var(--cp-text-dim,#64748b); margin-top:2px; }
      .hint { font-size:var(--cp-fs-sm,12px); margin-top:var(--cp-gap-xs,4px); }
      .hint.algo { color:var(--cp-warn,#d97706); }
      .hint.contact { color:var(--cp-accent,#4f46e5); }
      .rbadge { display:inline-block; margin-top:var(--cp-gap-xs,4px); padding:2px 8px;
                border-radius:99px; font-size:var(--cp-fs-tiny,11px);
                background:var(--cp-accent-weak,rgba(79,70,229,.1)); color:var(--cp-accent,#4f46e5); }
      .rbadge.warn { background:rgba(217,119,6,.14); color:var(--cp-warn,#d97706); }
      .rbadge.danger { background:rgba(220,38,38,.12); color:var(--cp-danger,#dc2626); }
      .acts { margin-top:var(--cp-gap-sm,6px); }
      button.warn { color:var(--cp-warn,#d97706); }`;
    }

    async fetchData(ctx) {
      return this._client.getRelStage({ conversationId: ctx.conversationId });
    }

    renderData(d) {
      const esc = (s) => this.esc(s);
      const pct = Math.max(0, Math.min(100, Math.round(d.progress_pct || 0)));
      const steps = (Array.isArray(d.stages) ? d.stages : [])
        .map((s) => {
          const cls = s.pending ? "step pending" : s.done ? "step done" : s.active ? "step active" : "step";
          return `<div class="${cls}" title="${esc(s.label)}${s.pending ? esc(this.t("cp.rel.pending_suffix")) : ""}"></div>`;
        }).join("");
      const intim = d.intimacy_score != null
        ? this.t("cp.rel.intimacy", { n: Math.round(d.intimacy_score) }) : this.t("cp.rel.intimacy_na");

      let contactHint = "";
      if (d.contact_stage_label) {
        contactHint = `<div class="hint contact">${esc(this.t("cp.rel.contact_level", { label: d.contact_stage_label }))}` +
          (d.contact_updated_by ? esc(this.t("cp.rel.updated_by", { by: d.contact_updated_by })) : "") + `</div>`;
      }
      const algoHint = (d.needs_confirmation && d.computed_stage_label)
        ? `<div class="hint algo">${esc(this.t("cp.rel.algo_suggest", { label: d.computed_stage_label }))}</div>` : "";

      let conflict = "";
      const acts = [];
      if (d.stage_conflict) {
        const detail = d.stage_conflict_detail || {};
        const reasons = (detail.reasons || []).join(this.t("cp.common.list_sep")) || this.t("cp.rel.multi_conflict");
        conflict = `<div class="rbadge warn">⚠ ${esc(reasons)}</div>`;
        const contactId = (d.context && d.context.contact_id) || "";
        if (contactId) {
          if (detail.show_to_contact !== false && detail.contact_stage) {
            acts.push(`<button data-act="sync_contact">${esc(this.t("cp.rel.sync_contact"))}</button>`);
          }
          if (detail.show_to_highest) {
            const hLbl = detail.highest_stage_label || detail.highest_stage || this.t("cp.rel.highest");
            acts.push(`<button class="primary" data-act="sync_highest">${esc(this.t("cp.rel.sync_highest", { label: hLbl }))}</button>`);
          }
          if (!detail.show_to_highest && !(detail.show_to_contact !== false && detail.contact_stage)) {
            acts.push(`<button data-act="sync_contact">${esc(this.t("cp.rel.sync_one"))}</button>`);
          }
        }
      }
      if (d.needs_confirmation) acts.push(`<button class="primary" data-act="confirm">${esc(this.t("cp.rel.confirm"))}</button>`);
      if (d.reunion) acts.push(`<button data-act="reunion">${esc(this.t("cp.rel.reunion"))}</button>`);
      if (d.confirmed_stage && d.confirmed_stage !== "initial") {
        acts.push(`<button class="warn" data-act="downgrade">${esc(this.t("cp.rel.downgrade"))}</button>`);
      }

      return (
        `<div class="stage">💞 ${esc(d.display_stage_label || d.stage_label || "—")}</div>` +
        contactHint + conflict +
        `<div class="track"><div class="bar" style="width:${pct}%"></div></div>` +
        (steps ? `<div class="steps">${steps}</div>` : "") +
        `<div class="meta"><span>${esc(this.t("cp.rel.progress", { pct }))}</span><span>${esc(this.t("cp.rel.rounds", { n: d.exchange_count || 0 }))}</span></div>` +
        `<div class="meta"><span>${esc(intim)}</span>` +
        (d.next_stage_label ? `<span>→ ${esc(d.next_stage_label)}</span>` : "") + `</div>` +
        algoHint +
        (d.pending_advancement ? `<div class="rbadge warn">${esc(this.t("cp.rel.pending_adv", { label: d.pending_stage_label || "" }))}</div>` : "") +
        (d.advancement_ready ? `<div class="rbadge">${esc(this.t("cp.rel.adv_ready"))}</div>` : "") +
        (d.reunion ? `<div class="rbadge danger">${esc(this.t("cp.rel.reunion_badge"))}</div>` : "") +
        (acts.length ? `<div class="acts">${acts.join("")}</div>` : "")
      );
    }

    async onAction(action, _el) {
      const ctx = this._ctx, d = this._d || {};
      if (!this._client || !ctx || !ctx.conversationId) return;
      const cid = ctx.conversationId;
      this.shadowRoot.querySelectorAll("button[data-act]").forEach((b) => (b.disabled = true));
      try {
        if (action === "confirm") {
          await this._client.confirmStage({ conversationId: cid });
        } else if (action === "reunion") {
          await this._client.reunionStage({ conversationId: cid });
        } else if (action === "downgrade") {
          const reason = (typeof prompt === "function") ? prompt(this.t("cp.rel.downgrade_prompt")) : "";
          if (!reason || !reason.trim()) {
            this.shadowRoot.querySelectorAll("button[data-act]").forEach((b) => (b.disabled = false));
            return;
          }
          await this._client.downgradeStage({ conversationId: cid, reason: reason.trim() });
        } else if (action === "sync_contact" || action === "sync_highest") {
          const contactId = (d.context && d.context.contact_id) || "";
          if (!contactId) return;
          await this._client.syncContactStage({
            contactId, mode: action === "sync_highest" ? "to_highest" : "to_contact",
          });
        }
        this.emit("cp-rel-changed", { action, conversationId: cid });
        this.refresh();
      } catch (e) {
        this.shadowRoot.querySelectorAll("button[data-act]").forEach((b) => (b.disabled = false));
      }
    }
  }

  if (!customElements.get("cp-rel-stage")) customElements.define("cp-rel-stage", CpRelStage);
})(typeof window !== "undefined" ? window : this);
