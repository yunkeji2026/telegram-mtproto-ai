"use strict";

// 壳层 renderer 用的桥：拿配置 + 各平台注入脚本的绝对 file:// 路径（webview preload 需绝对路径）。
const { contextBridge, ipcRenderer } = require("electron");
const path = require("path");
const { pathToFileURL } = require("url");

function injectUrl(file) {
  return pathToFileURL(path.join(__dirname, "inject", file)).toString();
}

contextBridge.exposeInMainWorld("shell", {
  getConfig: () => ipcRenderer.invoke("desktop:config"),
  applyWhatsappUa: (args) => ipcRenderer.invoke("desktop:apply-whatsapp-ua", args),
  backendHealth: () => ipcRenderer.invoke("desktop:backend-health"),
  backendSpawnStatus: () => ipcRenderer.invoke("desktop:backend-spawn-status"),
  saveConfig: (patch) => ipcRenderer.invoke("desktop:save-config", patch),
  // P0-1 首启向导：AI Key 状态 / 测试 / 保存（写后端 overlay，热生效）
  setupAiStatus: () => ipcRenderer.invoke("desktop:setup-ai-status"),
  setupTestAi: (body) => ipcRenderer.invoke("desktop:setup-test-ai", body),
  setupSaveAiKey: (body) => ipcRenderer.invoke("desktop:setup-save-ai-key", body),
  injectUrl,
  // P2 业务右栏：renderer → 主进程 → 后端（CSP 安全）
  profile: (args) => ipcRenderer.invoke("desktop:profile", args),
  kbSearch: (args) => ipcRenderer.invoke("desktop:kb-search", args),
  templates: () => ipcRenderer.invoke("desktop:templates"),
  personas: () => ipcRenderer.invoke("desktop:personas"),
  personaBindings: () => ipcRenderer.invoke("desktop:persona-bindings"),
  personaBind: (args) => ipcRenderer.invoke("desktop:persona-bind", args),
  guardCheck: (args) => ipcRenderer.invoke("desktop:guard-check", args),
  personaUnbind: (args) => ipcRenderer.invoke("desktop:persona-unbind", args),
  smartReply: (args) => ipcRenderer.invoke("desktop:smart-reply", args),
  translate: (args) => ipcRenderer.invoke("desktop:translate", args),
  relStage: (args) => ipcRenderer.invoke("desktop:rel-stage", args),
  relConfirm: (args) => ipcRenderer.invoke("desktop:rel-confirm", args),
  relDowngrade: (args) => ipcRenderer.invoke("desktop:rel-downgrade", args),
  relReunion: (args) => ipcRenderer.invoke("desktop:rel-reunion", args),
  relSync: (args) => ipcRenderer.invoke("desktop:rel-sync", args),
  nbaList: (args) => ipcRenderer.invoke("desktop:nba-list", args),
  nbaExec: (args) => ipcRenderer.invoke("desktop:nba-exec", args),
  scriptList: (args) => ipcRenderer.invoke("desktop:script-list", args),
  startChain: (args) => ipcRenderer.invoke("desktop:start-chain", args),
  collabContext: (args) => ipcRenderer.invoke("desktop:collab-context", args),
  chainExecutions: (args) => ipcRenderer.invoke("desktop:chain-executions", args),
  chainCancel: (args) => ipcRenderer.invoke("desktop:chain-cancel", args),
  accountsList: () => ipcRenderer.invoke("desktop:accounts-list"),
  platformModes: (args) => ipcRenderer.invoke("desktop:platform-modes", args),
  loginStart: (args) => ipcRenderer.invoke("desktop:login-start", args),
  loginStatus: (args) => ipcRenderer.invoke("desktop:login-status", args),
  loginCancel: (args) => ipcRenderer.invoke("desktop:login-cancel", args),
  accountStart: (args) => ipcRenderer.invoke("desktop:account-start", args),
  accountStop: (args) => ipcRenderer.invoke("desktop:account-stop", args),
  setAutoReply: (args) => ipcRenderer.invoke("desktop:account-auto-reply", args),
  setAccountOverride: (args) => ipcRenderer.invoke("desktop:account-auto-reply-override", args),
  autoReplyAudit: (args) => ipcRenderer.invoke("desktop:auto-reply-audit", args),
  autoReplyConfig: () => ipcRenderer.invoke("desktop:auto-reply-config-get"),
  autoReplyHealth: () => ipcRenderer.invoke("desktop:auto-reply-health"),
  autoReplyWebhooks: () => ipcRenderer.invoke("desktop:auto-reply-webhooks-get"),
  setAutoReplyWebhooks: (list) => ipcRenderer.invoke("desktop:auto-reply-webhooks-set", list),
  testAutoReplyWebhook: (payload) => ipcRenderer.invoke("desktop:auto-reply-webhooks-test", payload),
  setAutoReplyConfig: (args) => ipcRenderer.invoke("desktop:auto-reply-config-set", args),
  analyze: (args) => ipcRenderer.invoke("desktop:analyze", args),
  thread: (args) => ipcRenderer.invoke("desktop:thread", args),
  copy: (text) => ipcRenderer.invoke("desktop:copy", text),
  // 原生系统通知（新私聊消息弹窗）：{title, body} → 主进程 Notification，点击聚焦主窗口。
  notify: (args) => ipcRenderer.invoke("desktop:notify", args),
  voiceProfiles: () => ipcRenderer.invoke("desktop:voice-profiles"),
  voiceTts: (args) => ipcRenderer.invoke("desktop:voice-tts", args),
  sendVoice: (body) => ipcRenderer.invoke("desktop:send-voice", body),
  voiceReconcile: () => ipcRenderer.invoke("desktop:voice-reconcile"),
  voicePurge: (body) => ipcRenderer.invoke("desktop:voice-purge", body),
  voicePurgeOrphans: () => ipcRenderer.invoke("desktop:voice-purge-orphans"),
  voiceUnbind: (args) => ipcRenderer.invoke("desktop:voice-unbind", args),
  voiceRebind: (body) => ipcRenderer.invoke("desktop:voice-rebind", body),
  voiceEnroll: (payload) => ipcRenderer.invoke("desktop:voice-enroll", payload),
  // D4b 受控出站桥：轮询取走发给本内嵌账号的全自动回复（已过后端 send-gate/kill-switch），
  // 在对应 webview 的官方页 DOM 填入并发送，再回执。
  outboundPull: (args) => ipcRenderer.invoke("desktop:outbound-pull", args),
  outboundAck: (args) => ipcRenderer.invoke("desktop:outbound-ack", args),
  // 受控出站人审介入：{id, action: cancel|hold|release|edit|retry, text?}
  outboundAction: (args) => ipcRenderer.invoke("desktop:outbound-action", args),
  // AI 重写助手：{id} → {ok, reply, original}
  outboundRewrite: (args) => ipcRenderer.invoke("desktop:outbound-rewrite", args),
  // 纠正样本导出：{source?, kind?} → {ok, path, count}
  exportCorrections: (args) => ipcRenderer.invoke("desktop:export-corrections", args),
  // 🩺 自动化健康看板（壳层聚合读，复用后端既有 API）：全账号注入健康 + 受控出站队列概览。
  injectHealthList: (args) => ipcRenderer.invoke("desktop:inject-health-list", args),
  outboundStats: (args) => ipcRenderer.invoke("desktop:outbound-stats", args),
  // 注入「持续失配」告警流（红点预警 + 告警块）。
  injectAlerts: (args) => ipcRenderer.invoke("desktop:inject-alerts", args),
  // D1 一键热修：打开覆写文件（系统默认编辑器）。
  openSelectors: () => ipcRenderer.invoke("desktop:open-selectors"),
  // D1 校验覆写文件（解析/被忽略字段反馈）。
  validateSelectors: () => ipcRenderer.invoke("desktop:validate-selectors"),
  setWindowTitle: (title) => ipcRenderer.invoke("desktop:set-title", title),
});
