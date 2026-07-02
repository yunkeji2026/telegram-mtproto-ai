"use strict";
/* 两端共享组件 · 协作上下文(<cp-collab>)— 继承 CpPanelBase
   对等网页端 _loadCollabContext:阶段/积分/运行中工作链 chips + 推荐话题(使用→cp-fill)
   + 跨会话同事注解数。只读 + 轻动作。
   用法:
     const el = document.createElement('cp-collab');
     el.client = CopilotShared.createCopilotClient();
     el.context = { conversationId };
   client 需实现:getCollabContext */
(function (root) {
  const Base = root.CopilotShared && root.CopilotShared.CpPanelBase;
  if (!Base) { console.error("cp-collab: CpPanelBase 未加载"); return; }

  class CpCollab extends Base {
    emptyText() { return this.t("cp.collab.empty"); }
    emptyDataText() { return this.t("cp.collab.no_data"); }
    errText() { return this.t("cp.collab.err"); }
    styles() {
      return `
      .chips { display:flex; gap:var(--cp-gap-xs,4px); flex-wrap:wrap; }
      .chip { display:inline-block; padding:2px 8px; border-radius:99px;
              font-size:var(--cp-fs-tiny,11px); background:var(--cp-surface-2,#f1f5f9);
              color:var(--cp-text-dim,#475569); }
      .chip.warn { background:rgba(217,119,6,.14); color:var(--cp-warn,#92400e); }
      .topics { margin-top:5px; font-size:var(--cp-fs-sm,12px); color:var(--cp-text,#475569); }
      .topic { cursor:pointer; text-decoration:underline; }
      .notes { margin-top:4px; font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8); }`;
    }

    async fetchData(ctx) {
      return this._client.getCollabContext({ conversationId: ctx.conversationId });
    }

    renderData(d) {
      const esc = (s) => this.esc(s);
      const rel = d.relationship || {};
      const stageLbl = d.contact_stage_label || rel.display_stage_label || rel.stage_label || "";
      const chains = (d.active_chains || []).length;
      const eng = d.engagement || {};
      const pts = eng.points != null ? this.t("cp.collab.points", { pts: esc(eng.points), level: esc(eng.level_name || "") }) : "";
      const topics = (d.suggested_topics || []).slice(0, 2);
      const notes = (d.recent_notes || []).length;

      const chips =
        (stageLbl ? `<span class="chip">${esc(this.t("cp.collab.stage", { stage: stageLbl }))}</span>` : "") +
        (d.stage_conflict ? `<span class="chip warn">${esc(this.t("cp.collab.stage_conflict"))}</span>` : "") +
        (pts ? `<span class="chip">${pts}</span>` : "") +
        (chains ? `<span class="chip">${esc(this.t("cp.collab.chains", { n: chains }))}</span>` : "");

      const topicsHtml = topics.length
        ? `<div class="topics">${esc(this.t("cp.collab.recommend"))}` + topics.map((t, i) =>
            `<span class="topic" data-act="fill" data-idx="${i}">${esc(t.title || "")}</span>`).join(" · ") + `</div>`
        : "";
      const notesHtml = notes ? `<div class="notes">${esc(this.t("cp.collab.notes", { n: notes }))}</div>` : "";
      this._topics = topics;
      return `<div class="chips">${chips || '<span class="chip">—</span>'}</div>` + topicsHtml + notesHtml;
    }

    onAction(act, el) {
      if (act !== "fill") return;
      const idx = parseInt(el.getAttribute("data-idx"), 10);
      const t = (this._topics || [])[idx];
      if (t && t.opener) this.emit("cp-fill", { text: t.opener, source: "collab" });
    }
  }

  if (!customElements.get("cp-collab")) customElements.define("cp-collab", CpCollab);
})(typeof window !== "undefined" ? window : this);
