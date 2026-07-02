"use strict";
/* 两端共享 · 语音克隆 / TTS / 发送（<cp-voice>）
   与统一收件箱 reply-tools + voice-enroll-panel 同源 API，布局适配侧栏纵向。
   用法:
     el.client = CopilotShared.createCopilotClient();
     el.context = { platform, accountId, chatKey, conversationId };
   监听 cp-fill 自动填入草稿文字。发送成功 emit cp-voice-sent。 */
(function (root) {
  const VOICE_KEY = "ws_voice_persona_v1";

  function fileToB64(file) {
    return new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => {
        const s = String(r.result || "");
        const i = s.indexOf(",");
        resolve(i >= 0 ? s.slice(i + 1) : s);
      };
      r.onerror = reject;
      r.readAsDataURL(file);
    });
  }

  class CpVoice extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: "open" });
      this._client = null;
      this._ctx = null;
      this._profiles = [];
      this._persona = "";
      this._enrollOpen = false;
      this._lastReconcile = null;
      this.shadowRoot.innerHTML = `<style>${this._css()}</style><div class="wrap empty">${this._t("cp.voice.empty")}</div>`;
      this.shadowRoot.addEventListener("click", (e) => this._onClick(e));
      this.shadowRoot.addEventListener("change", (e) => this._onChange(e));
      this.addEventListener("cp-fill", (e) => {
        const t = (e.detail && e.detail.text) || "";
        if (!t) return;
        const ta = this.shadowRoot.querySelector('[data-role="text"]');
        if (ta) ta.value = t;
      });
    }

    _css() {
      return `
      :host { display:block; font-size:var(--cp-fs-sm,12px); color:var(--cp-text,#e2e8f0); }
      .wrap { background:var(--cp-surface-2,#1a2332); border:1px solid var(--cp-border,#2a3544);
              border-radius:var(--cp-radius-sm,8px); padding:8px; }
      .empty { color:var(--cp-text-tiny,#94a3b8); text-align:center; padding:12px 4px; }
      .row { display:flex; flex-wrap:wrap; gap:6px; align-items:center; margin-bottom:6px; }
      select, input[type=text], textarea { font:inherit; font-size:var(--cp-fs-sm,12px);
        padding:4px 6px; border:1px solid var(--cp-border,#2a3544); border-radius:6px;
        background:var(--cp-surface,#0f1419); color:var(--cp-text,#e2e8f0); }
      textarea { width:100%; min-height:52px; resize:vertical; box-sizing:border-box; }
      button { font:inherit; font-size:var(--cp-fs-tiny,11px); cursor:pointer; padding:4px 8px;
        border:1px solid var(--cp-border,#2a3544); border-radius:6px;
        background:var(--cp-surface,#1a2332); color:var(--cp-text,#e2e8f0); }
      button.primary { background:var(--cp-accent,#3aa0ff); color:#fff; border-color:transparent; }
      button:disabled { opacity:.5; cursor:default; }
      .hint { font-size:var(--cp-fs-tiny,11px); color:var(--cp-text-tiny,#94a3b8); margin:4px 0; }
      .preview { margin-top:6px; padding:6px; border:1px dashed var(--cp-border,#2a3544); border-radius:6px; }
      audio { width:100%; margin-top:4px; }
      .panel { margin-top:8px; padding-top:8px; border-top:1px dashed var(--cp-border,#2a3544); }
      .panel h5 { margin:0 0 6px; font-size:12px; font-weight:600; }
      .recon { max-height:120px; overflow:auto; font-size:11px; color:var(--cp-text-dim,#94a3b8); }
      .ok { color:var(--cp-ok,#16a34a); } .err { color:var(--cp-danger,#dc2626); }`;
    }

    set client(c) { this._client = c; }
    get client() { return this._client; }
    set context(ctx) {
      this._ctx = ctx;
      this._persona = this._loadPersona();
      this._render();
      this._loadProfiles();
    }
    get context() { return this._ctx; }

    _convKey() {
      const c = this._ctx || {};
      return c.conversationId || `${c.platform || ""}:${c.accountId || ""}:${c.chatKey || ""}`;
    }
    _loadPersona() {
      try {
        const m = JSON.parse(localStorage.getItem(VOICE_KEY) || "{}");
        return m[this._convKey()] || "";
      } catch (e) { return ""; }
    }
    _savePersona(v) {
      try {
        const m = JSON.parse(localStorage.getItem(VOICE_KEY) || "{}");
        m[this._convKey()] = v;
        localStorage.setItem(VOICE_KEY, JSON.stringify(m));
      } catch (e) { /* */ }
    }

    _esc(s) {
      return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    }
    _t(key, vars) {
      const f = root.CopilotShared && root.CopilotShared.t;
      return f ? f(key, vars) : key;
    }

    _metaLine(d) {
      const m = (d && d.voice_meta) || {};
      const parts = [];
      if (m.persona_id) parts.push(`${this._t("cp.voice.m_persona")} ${m.persona_id}`);
      if (m.provider) parts.push(`${this._t("cp.voice.m_provider")} ${m.provider}`);
      if (m.voice) parts.push(`${this._t("cp.voice.m_voice")} ${m.voice}`);
      if (m.emotion) parts.push(`${this._t("cp.voice.m_emotion")} ${m.emotion}`);
      if (m.fallback_from) parts.push(this._t("cp.voice.m_fallback", { from: m.fallback_from }));
      return parts.length
        ? `<div class="hint">${this._t("cp.voice.m_actual", { parts: this._esc(parts.join(" · ")) })}</div>`
        : "";
    }

    async _loadProfiles() {
      if (!this._client || !this._client.voiceProfiles) return;
      try {
        const d = await this._client.voiceProfiles();
        if (!d || !d.ok) return;
        this._profiles = d.profiles || [];
        const sel = this.shadowRoot.querySelector('[data-role="persona"]');
        if (!sel) return;
        let html = `<option value="">${this._t("cp.voice.default_voice")}</option>`;
        const dft = d.default || {};
        if (dft.is_clone) html = `<option value="">${this._t("cp.voice.default_voice_clone")}</option>`;
        this._profiles.forEach((p) => {
          const tag = p.is_clone ? (p.ready ? " 🎤" : " 🎤⚠") : "";
          const dis = p.is_clone && !p.ready ? " disabled" : "";
          html += `<option value="${this._esc(p.persona_id)}"${dis}>${this._esc(p.name)}${tag}</option>`;
        });
        sel.innerHTML = html;
        const ok = Array.from(sel.options).some((o) => o.value === this._persona && !o.disabled);
        sel.value = ok ? this._persona : "";
      } catch (e) { /* */ }
    }

    _render() {
      const w = this.shadowRoot.querySelector(".wrap");
      if (!this._ctx || !this._ctx.chatKey) {
        w.className = "wrap empty";
        w.textContent = this._t("cp.voice.empty");
        return;
      }
      w.className = "wrap";
      w.innerHTML =
        `<div class="row">
          <button class="primary" data-act="tts">${this._t("cp.voice.tts_btn")}</button>
          <select data-role="persona" title="${this._esc(this._t("cp.voice.persona_title"))}"></select>
          <button data-act="unbind" title="${this._esc(this._t("cp.voice.unbind_title"))}">🗑</button>
          <button data-act="toggle-enroll" title="${this._esc(this._t("cp.voice.enroll_title"))}">${this._t("cp.voice.enroll_btn")}</button>
        </div>
        <textarea data-role="text" placeholder="${this._esc(this._t("cp.voice.text_ph"))}"></textarea>
        <div class="hint">${this._t("cp.voice.hint")}</div>
        <div data-role="preview" class="preview" hidden></div>
        <div data-role="enroll" class="panel" hidden>${this._enrollHtml()}</div>`;
      if (this._enrollOpen) {
        const ep = this.shadowRoot.querySelector('[data-role="enroll"]');
        if (ep) ep.hidden = false;
        this._loadEnrollPersonas();
      }
    }

    _enrollHtml() {
      return `<h5>${this._t("cp.voice.enroll_h")}</h5>
        <div class="row">
          <input type="file" data-role="efile" accept="audio/*,.wav,.mp3,.m4a" />
          <input type="text" data-role="ename" placeholder="${this._esc(this._t("cp.voice.ename_ph"))}" />
          <select data-role="epersona"><option value="">${this._t("cp.voice.target_persona_opt")}</option></select>
          <select data-role="elang"><option value="Japanese">${this._t("cp.lang.ja")}</option><option value="Chinese">${this._t("cp.lang.zh")}</option><option value="English">${this._t("cp.lang.en")}</option></select>
          <button data-act="enroll-submit">${this._t("cp.voice.enroll_submit")}</button>
        </div>
        <div class="row">
          <input type="text" data-role="ereftext" placeholder="${this._esc(this._t("cp.voice.reftext_ph"))}" style="flex:1;" />
        </div>
        <div data-role="ehint" class="hint">${this._t("cp.voice.enroll_hint")}</div>
        <div data-role="audition"></div>
        <div class="panel">
          <h5>${this._t("cp.voice.reuse_h")}</h5>
          <div class="row">
            <select data-role="rfrom"><option value="">${this._t("cp.voice.src_persona_opt")}</option></select>
            <span>→</span>
            <select data-role="rto"><option value="">${this._t("cp.voice.dst_persona_opt")}</option></select>
            <button data-act="rebind">${this._t("cp.voice.copy_btn")}</button>
          </div>
        </div>
        <div class="panel">
          <h5>${this._t("cp.voice.recon_h")}</h5>
          <div class="row">
            <button data-act="reconcile">${this._t("cp.voice.recon_btn")}</button>
            <button data-act="purge-orphans">${this._t("cp.voice.purge_btn")}</button>
          </div>
          <div data-role="recon" class="recon"></div>
        </div>`;
    }

    async _loadEnrollPersonas() {
      if (!this._client || !this._client.listPersonas) return;
      try {
        const d = await this._client.listPersonas();
        const list = (d && d.summary) || [];
        const fill = (sel, filterVoice) => {
          if (!sel) return;
          let h = sel === this.shadowRoot.querySelector('[data-role="epersona"]')
            ? `<option value="">${this._t("cp.voice.target_persona_opt")}</option>`
            : (sel.getAttribute("data-role") === "rfrom" ? `<option value="">${this._t("cp.voice.src_persona_opt")}</option>` : `<option value="">${this._t("cp.voice.dst_persona_opt")}</option>`);
          list.filter((s) => !filterVoice || s.has_voice).forEach((s) => {
            h += `<option value="${this._esc(s.id)}">${this._esc(s.name || s.id)}${s.has_voice ? " 🎤" : ""}</option>`;
          });
          sel.innerHTML = h;
        };
        fill(this.shadowRoot.querySelector('[data-role="epersona"]'), false);
        fill(this.shadowRoot.querySelector('[data-role="rfrom"]'), true);
        fill(this.shadowRoot.querySelector('[data-role="rto"]'), false);
      } catch (e) { /* */ }
    }

    _onChange(e) {
      const sel = e.target.closest('[data-role="persona"]');
      if (sel) {
        this._persona = sel.value;
        this._savePersona(this._persona);
      }
    }

    async _onClick(e) {
      const b = e.target.closest("[data-act]");
      if (!b) return;
      const act = b.getAttribute("data-act");
      if (act === "tts") return this._genTts();
      if (act === "send") return this._sendVoice();
      if (act === "unbind") return this._unbind();
      if (act === "toggle-enroll") {
        this._enrollOpen = !this._enrollOpen;
        const ep = this.shadowRoot.querySelector('[data-role="enroll"]');
        if (ep) {
          ep.hidden = !this._enrollOpen;
          if (this._enrollOpen) {
            ep.innerHTML = this._enrollHtml();
            this._loadEnrollPersonas();
            this._reconcile();
          }
        }
        return;
      }
      if (act === "enroll-submit") return this._enroll();
      if (act === "rebind") return this._rebind();
      if (act === "reconcile") return this._reconcile();
      if (act === "purge-orphans") return this._purgeOrphans();
      if (act === "purge-one") return this._purgeOne(b.getAttribute("data-voice"), b.getAttribute("data-force") === "1");
    }

    _text() {
      const ta = this.shadowRoot.querySelector('[data-role="text"]');
      return ta ? String(ta.value || "").trim() : "";
    }

    async _genTts() {
      const text = this._text();
      if (!text) { this._hint(this._t("cp.voice.need_text")); return; }
      const box = this.shadowRoot.querySelector('[data-role="preview"]');
      if (!box) return;
      box.hidden = false;
      box.textContent = this._t("cp.voice.gen_audition");
      try {
        const d = await this._client.voiceTts({ text, persona_id: this._persona || undefined });
        const url = d.dataUrl || d.audio_url ||
          (d.filename ? `/api/voice/tts-file/${encodeURIComponent(d.filename)}` : "");
        if (!url && !d.ok) {
          box.innerHTML = `<span class="err">${this._esc(this._t("cp.voice.gen_fail", { msg: d.message || d.error || this._t("cp.voice.tts_unavailable") }))}</span>`;
          return;
        }
        box.innerHTML =
          `${this._t("cp.voice.preview")}<br><audio controls src="${url}"></audio>` +
          this._metaLine(d) +
          `<div class="row" style="justify-content:flex-end;margin-top:6px;">` +
          `<button class="primary" data-act="send">${this._t("cp.voice.send_btn")}</button></div>`;
      } catch (e) {
        box.innerHTML = `<span class="err">${this._esc(this._t("cp.voice.req_fail"))}</span>`;
      }
    }

    async _sendVoice() {
      const c = this._ctx;
      const text = this._text();
      if (!text || !c) return;
      try {
        const d = await this._client.sendVoice({
          platform: c.platform,
          account_id: c.accountId || "default",
          chat_key: c.chatKey,
          text,
          persona_id: this._persona || undefined,
        });
        if (d && d.ok) {
          const m = d.voice_meta || {};
          this._hint(
            this._t("cp.voice.sent")
              + (m.provider ? ` (${m.provider}${m.emotion ? " / " + m.emotion : ""})` : ""),
            true);
          this.dispatchEvent(new CustomEvent("cp-voice-sent", { bubbles: true, composed: true }));
        } else {
          this._hint(this._t("cp.voice.send_fail", { msg: d.message || d.detail || d.reason || this._t("cp.voice.need_online") }), false);
        }
      } catch (e) { this._hint(this._t("cp.voice.send_req_fail"), false); }
    }

    async _unbind() {
      if (!this._persona) { this._hint(this._t("cp.voice.need_pick_unbind")); return; }
      if (!confirm(this._t("cp.voice.unbind_confirm"))) return;
      const purge = confirm(this._t("cp.voice.purge_cloud_confirm"));
      try {
        const d = await this._client.voiceUnbind({ persona_id: this._persona, purge_cloud: purge });
        if (d && d.ok) {
          this._hint(this._t("cp.voice.unbound"));
          await this._loadProfiles();
        } else this._hint(d.message || this._t("cp.voice.unbind_fail"));
      } catch (e) { this._hint(this._t("cp.voice.unbind_fail")); }
    }

    async _enroll() {
      const file = this.shadowRoot.querySelector('[data-role="efile"]');
      const name = (this.shadowRoot.querySelector('[data-role="ename"]').value || "").trim();
      const persona = this.shadowRoot.querySelector('[data-role="epersona"]').value;
      const lang = this.shadowRoot.querySelector('[data-role="elang"]').value;
      const refEl = this.shadowRoot.querySelector('[data-role="ereftext"]');
      const refText = ((refEl && refEl.value) || "").trim();
      const hint = this.shadowRoot.querySelector('[data-role="ehint"]');
      if (!file || !file.files || !file.files[0]) { hint.textContent = this._t("cp.voice.need_file"); return; }
      if (!name || !persona) { hint.textContent = this._t("cp.voice.need_name_persona"); return; }
      hint.textContent = this._t("cp.voice.enrolling");
      try {
        let d;
        if (root.shell && root.shell.voiceEnroll) {
          const b64 = await fileToB64(file.files[0]);
          d = await this._client.voiceEnroll({
            audio_b64: b64, filename: file.files[0].name,
            persona_id: persona, preferred_name: name, language_type: lang,
            reference_text: refText,
          });
        } else {
          d = await this._client.voiceEnroll({
            file: file.files[0], persona_id: persona, preferred_name: name, language_type: lang,
            reference_text: refText,
          });
        }
        if (d && d.ok) {
          hint.textContent = this._t("cp.voice.enroll_ok");
          this._persona = persona;
          this._savePersona(persona);
          await this._loadProfiles();
          const sel = this.shadowRoot.querySelector('[data-role="persona"]');
          if (sel) sel.value = persona;
          await this._audition(persona);
        } else hint.textContent = "❌ " + (d.message || d.reason || this._t("cp.voice.enroll_failed"));
      } catch (e) { hint.textContent = "❌ " + this._t("cp.voice.req_fail"); }
    }

    async _audition(persona_id) {
      const box = this.shadowRoot.querySelector('[data-role="audition"]');
      if (!box) return;
      box.textContent = this._t("cp.voice.gen_audition");
      try {
        const d = await this._client.voiceTts({ text: this._t("cp.voice.audition_sample"), persona_id });
        const url = d.dataUrl || d.audio_url || "";
        box.innerHTML = url
          ? `🔊 <audio controls src="${url}" style="max-width:100%;"></audio>${this._metaLine(d)}`
          : this._t("cp.voice.audition_fail");
      } catch (e) { box.textContent = this._t("cp.voice.audition_unavailable"); }
    }

    async _rebind() {
      const from = this.shadowRoot.querySelector('[data-role="rfrom"]').value;
      const to = this.shadowRoot.querySelector('[data-role="rto"]').value;
      if (!from || !to || from === to) return;
      const d = await this._client.voiceRebind({ from_persona_id: from, to_persona_id: to });
      if (d && d.ok) { await this._loadProfiles(); await this._audition(to); }
    }

    async _reconcile() {
      const box = this.shadowRoot.querySelector('[data-role="recon"]');
      if (!box) return;
      box.textContent = this._t("cp.voice.reconciling");
      try {
        const d = await this._client.voiceReconcile();
        this._lastReconcile = d;
        const s = d.summary || {};
        let html = this._esc(this._t("cp.voice.recon_summary", {
          cloud: s.cloud_total || 0, local: s.local_voice_ids || 0, orphan: s.orphan_count || 0,
        }));
        if (!(d.orphans || []).length && !(d.dangling || []).length) html += ` <span class="ok">${this._t("cp.voice.recon_ok")}</span>`;
        box.innerHTML = html;
      } catch (e) { box.textContent = this._t("cp.voice.recon_fail"); }
    }

    async _purgeOrphans() {
      if (!confirm(this._t("cp.voice.purge_orphans_confirm"))) return;
      await this._client.voicePurgeOrphans();
      await this._reconcile();
    }

    async _purgeOne(voice, force) {
      if (!voice) return;
      await this._client.voicePurge({ voice, force: !!force });
      await this._reconcile();
    }

    _hint(msg, ok) {
      const h = this.shadowRoot.querySelector(".hint");
      if (h) {
        h.textContent = msg;
        h.className = ok ? "hint ok" : "hint err";
      }
    }
  }

  if (!customElements.get("cp-voice")) customElements.define("cp-voice", CpVoice);
})(typeof window !== "undefined" ? window : this);
