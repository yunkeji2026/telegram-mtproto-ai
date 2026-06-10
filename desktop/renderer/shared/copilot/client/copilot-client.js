"use strict";
/* 两端共享 · 数据适配层。组件只依赖统一接口,不关心传输:
   - 桌面:window.shell.*  → IPC → main.js → 后端(规避 CSP/CORS)
   - 网页:fetch('/api/...') 同源带 session
   经典脚本,挂 window.CopilotShared,不引外链(满足 CSP 'self')。 */
(function (root) {
  // 复刻后端 conv_id 公式:src/inbox/normalizer.py::conv_id
  function conversationId(platform, accountId, chatKey) {
    return `${platform}:${accountId}:${chatKey}`;
  }

  // 可选鉴权 token:浏览器靠同源 cookie,桌面 iframe 由壳注入 token(同一 WebClient 跨端)。
  let _authToken = "";
  function setAuthToken(t) { _authToken = String(t || ""); }
  function _authHeaders(base) {
    const h = Object.assign({}, base || {});
    if (_authToken) h["Authorization"] = `Bearer ${_authToken}`;
    return h;
  }

  // —— 网页适配器:同源 fetch ——
  class WebCopilotClient {
    async _get(url) {
      const r = await fetch(url, { headers: _authHeaders() });
      return await r.json();
    }
    async _post(url, body) {
      const r = await fetch(url, {
        method: "POST",
        headers: _authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(body || {}),
      });
      return await r.json();
    }
    async getRelStage({ conversationId: cid }) {
      if (!cid) return { ok: false, error: "missing conversationId" };
      return this._get(`/api/workspace/conv/${encodeURIComponent(cid)}/relationship-stage`);
    }
    async confirmStage({ conversationId: cid }) {
      return this._post(`/api/workspace/conv/${encodeURIComponent(cid)}/relationship-stage/confirm`, {});
    }
    async downgradeStage({ conversationId: cid, reason }) {
      return this._post(`/api/workspace/conv/${encodeURIComponent(cid)}/relationship-stage/downgrade`, { reason });
    }
    async reunionStage({ conversationId: cid }) {
      return this._post(`/api/workspace/conv/${encodeURIComponent(cid)}/relationship-stage/reunion`, {});
    }
    async syncContactStage({ contactId, mode }) {
      return this._post(`/api/workspace/contact/${encodeURIComponent(contactId)}/relationship-stage/sync`, { mode: mode || "to_contact" });
    }
    async getNextActions({ conversationId: cid }) {
      if (!cid) return { ok: false, error: "missing conversationId" };
      return this._get(`/api/workspace/conv/${encodeURIComponent(cid)}/next-actions`);
    }
    async executeAction({ conversationId: cid, action_id, action_type, config }) {
      return this._post(`/api/workspace/conv/${encodeURIComponent(cid)}/execute-action`, { action_id, action_type, config: config || {} });
    }
    async getScriptTopics({ conversationId: cid }) {
      if (!cid) return { ok: false, error: "missing conversationId" };
      return this._get(`/api/workspace/conv/${encodeURIComponent(cid)}/script-suggestions`);
    }
    async startChain({ conversationId: cid, chainId }) {
      return this._post(`/api/workspace/conv/${encodeURIComponent(cid)}/start-chain`, { chain_id: chainId });
    }
    async listPersonas() {
      return this._get(`/api/personas/profiles`);
    }
    async getPersonaBindings() {
      return this._get(`/api/persona/bindings`);
    }
    async bindPersona({ chatKey, persona }) {
      return this._post(`/api/persona/bind`, { chat_id: chatKey, persona });
    }
    async unbindPersona({ chatKey }) {
      return this._post(`/api/persona/unbind`, { chat_id: chatKey });
    }
    async getCollabContext({ conversationId: cid }) {
      if (!cid) return { ok: false, error: "missing conversationId" };
      return this._get(`/api/workspace/conv/${encodeURIComponent(cid)}/collab-context`);
    }
    async getChainExecutions({ conversationId: cid, limit }) {
      if (!cid) return { ok: false, error: "missing conversationId" };
      return this._get(`/api/workspace/conv/${encodeURIComponent(cid)}/chain-executions?limit=${encodeURIComponent(limit || 8)}`);
    }
    async cancelChainExecution({ execId }) {
      return this._post(`/api/workspace/chain-executions/${encodeURIComponent(execId)}/cancel`, {});
    }
    async getHistory({ conversationId: cid, limit }) {
      if (!cid) return { ok: false, error: "missing conversationId" };
      return this._get(`/api/unified-inbox/history?conversation_id=${encodeURIComponent(cid)}&limit=${encodeURIComponent(limit || 30)}`);
    }
    async smartReply(payload) {
      return this._post(`/api/desktop/smart-reply`, payload || {});
    }
    async guardCheck({ text }) {
      return this._post(`/api/desktop/guard-check`, { text });
    }
    // —— 账号管理（Phase 2，两端共用）——
    async listAccounts() {
      return this._get(`/api/accounts`);
    }
    async getPlatformModes({ platform }) {
      return this._get(`/api/platforms/${encodeURIComponent(platform)}/modes`);
    }
    async startLogin({ platform, mode, account_id, label, proxy_id, use_fingerprint }) {
      return this._post(`/api/platforms/${encodeURIComponent(platform)}/login/start`,
        { mode, account_id, label, proxy_id, use_fingerprint });
    }
    async loginStatus({ platform, login_id }) {
      return this._get(`/api/platforms/${encodeURIComponent(platform)}/login/${encodeURIComponent(login_id)}/status`);
    }
    async cancelLogin({ platform, login_id }) {
      return this._post(`/api/platforms/${encodeURIComponent(platform)}/login/${encodeURIComponent(login_id)}/cancel`, {});
    }
    async accountStart({ platform, account_id }) {
      return this._post(`/api/accounts/${encodeURIComponent(platform)}/${encodeURIComponent(account_id)}/start`, {});
    }
    async accountStop({ platform, account_id }) {
      return this._post(`/api/accounts/${encodeURIComponent(platform)}/${encodeURIComponent(account_id)}/stop`, {});
    }
    async setAutoReply({ platform, account_id, enabled }) {
      return this._post(`/api/accounts/${encodeURIComponent(platform)}/${encodeURIComponent(account_id)}/auto-reply`, { enabled: !!enabled });
    }
    async setAccountOverride({ platform, account_id, override }) {
      return this._post(`/api/accounts/${encodeURIComponent(platform)}/${encodeURIComponent(account_id)}/auto-reply/override`, override || {});
    }
    async autoReplyAudit({ limit, platform, account_id, since } = {}) {
      const qs = new URLSearchParams();
      if (limit != null) qs.set("limit", String(limit));
      if (platform) qs.set("platform", platform);
      if (account_id) qs.set("account_id", account_id);
      if (since != null) qs.set("since", String(since));
      const q = qs.toString();
      return this._get(`/api/accounts/auto-reply/audit${q ? "?" + q : ""}`);
    }
    async autoReplyConfig() {
      return this._get(`/api/accounts/auto-reply/config`);
    }
    async autoReplyHealth() {
      return this._get(`/api/accounts/auto-reply/health`);
    }
    async autoReplyWebhooks() {
      return this._get(`/api/accounts/auto-reply/webhooks`);
    }
    async setAutoReplyWebhooks(list) {
      return this._post(`/api/accounts/auto-reply/webhooks`, { webhooks: list || [] });
    }
    async testAutoReplyWebhook(payload) {
      return this._post(`/api/accounts/auto-reply/webhooks/test`, payload || {});
    }
    async setAutoReplyConfig(settings) {
      return this._post(`/api/accounts/auto-reply/config`, settings || {});
    }
    // SSE 实时流（同源带 cookie）。鉴权/CSP 失败 → onError 回落轮询。
    openAuditStream(onItem, onError) {
      if (typeof EventSource === "undefined") return null;
      let es;
      try { es = new EventSource(`/api/accounts/auto-reply/stream`); }
      catch (e) { return null; }
      es.onmessage = (ev) => {
        try { const d = JSON.parse(ev.data); if (d && d.id) onItem(d); } catch (e) { /* */ }
      };
      es.onerror = () => { try { es.close(); } catch (e) { /* */ } if (onError) onError(); };
      return es;
    }
    // —— 语音克隆 / TTS（与统一收件箱同源）——
    async voiceProfiles() { return this._get("/api/voice/profiles"); }
    async voiceTts({ text, persona_id }) {
      return this._post("/api/voice/tts-test", { text, persona_id: persona_id || undefined });
    }
    async sendVoice(body) { return this._post("/api/unified-inbox/send-voice", body || {}); }
    async voiceReconcile() { return this._get("/api/voice/reconcile"); }
    async voicePurge(body) { return this._post("/api/voice/purge", body || {}); }
    async voicePurgeOrphans() { return this._post("/api/voice/purge-orphans", {}); }
    async voiceUnbind({ persona_id, purge_cloud }) {
      const q = purge_cloud ? "?purge_cloud=1" : "";
      const r = await fetch(`/api/voice/profiles/${encodeURIComponent(persona_id || "")}${q}`, {
        method: "DELETE", headers: _authHeaders(),
      });
      return await r.json();
    }
    async voiceRebind(body) { return this._post("/api/voice/rebind", body || {}); }
    async voiceEnroll(payload) {
      const fd = new FormData();
      const p = payload || {};
      if (p.file) fd.append("file", p.file);
      fd.append("persona_id", String(p.persona_id || ""));
      fd.append("preferred_name", String(p.preferred_name || ""));
      fd.append("language_type", String(p.language_type || "Japanese"));
      const r = await fetch("/api/voice/enroll", { method: "POST", headers: _authHeaders(), body: fd });
      return await r.json();
    }
  }

  // —— 桌面适配器:经 window.shell IPC ——
  class DesktopCopilotClient {
    _shell() { return root.shell || {}; }
    async getRelStage({ conversationId: cid }) {
      if (!cid) return { ok: false, error: "missing conversationId" };
      const s = this._shell();
      return s.relStage ? s.relStage({ conversation_id: cid }) : { ok: false, error: "shell.relStage 未暴露" };
    }
    async confirmStage({ conversationId: cid }) {
      const s = this._shell();
      return s.relConfirm ? s.relConfirm({ conversation_id: cid }) : { ok: false };
    }
    async downgradeStage({ conversationId: cid, reason }) {
      const s = this._shell();
      return s.relDowngrade ? s.relDowngrade({ conversation_id: cid, reason }) : { ok: false };
    }
    async reunionStage({ conversationId: cid }) {
      const s = this._shell();
      return s.relReunion ? s.relReunion({ conversation_id: cid }) : { ok: false };
    }
    async syncContactStage({ contactId, mode }) {
      const s = this._shell();
      return s.relSync ? s.relSync({ contact_id: contactId, mode: mode || "to_contact" }) : { ok: false };
    }
    async getNextActions({ conversationId: cid }) {
      if (!cid) return { ok: false, error: "missing conversationId" };
      const s = this._shell();
      return s.nbaList ? s.nbaList({ conversation_id: cid }) : { ok: false, error: "shell.nbaList 未暴露" };
    }
    async executeAction({ conversationId: cid, action_id, action_type, config }) {
      const s = this._shell();
      return s.nbaExec ? s.nbaExec({ conversation_id: cid, action_id, action_type, config: config || {} }) : { ok: false };
    }
    async getScriptTopics({ conversationId: cid }) {
      if (!cid) return { ok: false, error: "missing conversationId" };
      const s = this._shell();
      return s.scriptList ? s.scriptList({ conversation_id: cid }) : { ok: false, error: "shell.scriptList 未暴露" };
    }
    async startChain({ conversationId: cid, chainId }) {
      const s = this._shell();
      return s.startChain ? s.startChain({ conversation_id: cid, chain_id: chainId }) : { ok: false };
    }
    async listPersonas() {
      const s = this._shell();
      return s.personas ? s.personas() : { ok: false, error: "shell.personas 未暴露" };
    }
    async getPersonaBindings() {
      const s = this._shell();
      return s.personaBindings ? s.personaBindings() : { ok: false, error: "shell.personaBindings 未暴露" };
    }
    async bindPersona({ chatKey, persona }) {
      const s = this._shell();
      return s.personaBind ? s.personaBind({ chat_id: chatKey, persona }) : { ok: false };
    }
    async unbindPersona({ chatKey }) {
      const s = this._shell();
      return s.personaUnbind ? s.personaUnbind({ chat_id: chatKey }) : { ok: false };
    }
    async getCollabContext({ conversationId: cid }) {
      if (!cid) return { ok: false, error: "missing conversationId" };
      const s = this._shell();
      return s.collabContext ? s.collabContext({ conversation_id: cid }) : { ok: false, error: "shell.collabContext 未暴露" };
    }
    async getChainExecutions({ conversationId: cid, limit }) {
      if (!cid) return { ok: false, error: "missing conversationId" };
      const s = this._shell();
      return s.chainExecutions ? s.chainExecutions({ conversation_id: cid, limit: limit || 8 }) : { ok: false, error: "shell.chainExecutions 未暴露" };
    }
    async cancelChainExecution({ execId }) {
      const s = this._shell();
      return s.chainCancel ? s.chainCancel({ exec_id: execId }) : { ok: false };
    }
    async getHistory({ conversationId: cid, limit }) {
      // 桌面壳按 platform/account/chat_key 取 live thread;按 conversation_id 自取留待 iframe 同源态用 Web 适配器
      const s = this._shell();
      return s.historyByConv ? s.historyByConv({ conversation_id: cid, limit: limit || 30 }) : { ok: false, error: "shell.historyByConv 未暴露" };
    }
    async smartReply(payload) {
      const s = this._shell();
      return s.smartReply ? s.smartReply(payload || {}) : { ok: false, error: "shell.smartReply 未暴露" };
    }
    async guardCheck({ text }) {
      const s = this._shell();
      return s.guardCheck ? s.guardCheck({ text }) : { ok: false };
    }
    // —— 账号管理（Phase 2，两端共用）——
    async listAccounts() {
      const s = this._shell();
      return s.accountsList ? s.accountsList() : { ok: false, error: "shell.accountsList 未暴露" };
    }
    async getPlatformModes({ platform }) {
      const s = this._shell();
      return s.platformModes ? s.platformModes({ platform }) : { ok: false, error: "shell.platformModes 未暴露" };
    }
    async startLogin(args) {
      const s = this._shell();
      return s.loginStart ? s.loginStart(args || {}) : { ok: false, error: "shell.loginStart 未暴露" };
    }
    async loginStatus(args) {
      const s = this._shell();
      return s.loginStatus ? s.loginStatus(args || {}) : { ok: false, error: "shell.loginStatus 未暴露" };
    }
    async cancelLogin(args) {
      const s = this._shell();
      return s.loginCancel ? s.loginCancel(args || {}) : { ok: false };
    }
    async accountStart(args) {
      const s = this._shell();
      return s.accountStart ? s.accountStart(args || {}) : { ok: false };
    }
    async accountStop(args) {
      const s = this._shell();
      return s.accountStop ? s.accountStop(args || {}) : { ok: false };
    }
    async setAutoReply(args) {
      const s = this._shell();
      return s.setAutoReply ? s.setAutoReply(args || {}) : { ok: false, error: "shell.setAutoReply 未暴露" };
    }
    async setAccountOverride(args) {
      const s = this._shell();
      return s.setAccountOverride ? s.setAccountOverride(args || {}) : { ok: false, error: "shell.setAccountOverride 未暴露" };
    }
    async autoReplyAudit(args) {
      const s = this._shell();
      return s.autoReplyAudit ? s.autoReplyAudit(args || {}) : { ok: false, error: "shell.autoReplyAudit 未暴露" };
    }
    async autoReplyConfig() {
      const s = this._shell();
      return s.autoReplyConfig ? s.autoReplyConfig() : { ok: false, error: "shell.autoReplyConfig 未暴露" };
    }
    async autoReplyHealth() {
      const s = this._shell();
      return s.autoReplyHealth ? s.autoReplyHealth() : { ok: false, error: "shell.autoReplyHealth 未暴露" };
    }
    async autoReplyWebhooks() {
      const s = this._shell();
      return s.autoReplyWebhooks ? s.autoReplyWebhooks() : { ok: false, error: "shell.autoReplyWebhooks 未暴露" };
    }
    async setAutoReplyWebhooks(list) {
      const s = this._shell();
      return s.setAutoReplyWebhooks ? s.setAutoReplyWebhooks(list || []) : { ok: false, error: "shell.setAutoReplyWebhooks 未暴露" };
    }
    async testAutoReplyWebhook(payload) {
      const s = this._shell();
      return s.testAutoReplyWebhook ? s.testAutoReplyWebhook(payload || {}) : { ok: false, error: "shell.testAutoReplyWebhook 未暴露" };
    }
    async setAutoReplyConfig(settings) {
      const s = this._shell();
      return s.setAutoReplyConfig ? s.setAutoReplyConfig(settings || {}) : { ok: false, error: "shell.setAutoReplyConfig 未暴露" };
    }
    async voiceProfiles() {
      const s = this._shell();
      return s.voiceProfiles ? s.voiceProfiles() : { ok: false };
    }
    async voiceTts(args) {
      const s = this._shell();
      return s.voiceTts ? s.voiceTts(args || {}) : { ok: false };
    }
    async sendVoice(body) {
      const s = this._shell();
      return s.sendVoice ? s.sendVoice(body || {}) : { ok: false };
    }
    async voiceReconcile() {
      const s = this._shell();
      return s.voiceReconcile ? s.voiceReconcile() : { ok: false };
    }
    async voicePurge(body) {
      const s = this._shell();
      return s.voicePurge ? s.voicePurge(body || {}) : { ok: false };
    }
    async voicePurgeOrphans() {
      const s = this._shell();
      return s.voicePurgeOrphans ? s.voicePurgeOrphans() : { ok: false };
    }
    async voiceUnbind(args) {
      const s = this._shell();
      return s.voiceUnbind ? s.voiceUnbind(args || {}) : { ok: false };
    }
    async voiceRebind(body) {
      const s = this._shell();
      return s.voiceRebind ? s.voiceRebind(body || {}) : { ok: false };
    }
    async voiceEnroll(payload) {
      const s = this._shell();
      return s.voiceEnroll ? s.voiceEnroll(payload || {}) : { ok: false, error: "shell.voiceEnroll 未暴露" };
    }
  }

  function createCopilotClient() {
    return root.shell ? new DesktopCopilotClient() : new WebCopilotClient();
  }

  root.CopilotShared = Object.assign(root.CopilotShared || {}, {
    conversationId,
    setAuthToken,
    WebCopilotClient,
    DesktopCopilotClient,
    createCopilotClient,
  });
})(typeof window !== "undefined" ? window : this);
