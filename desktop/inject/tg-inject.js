"use strict";

// 桌面 webview preload —— Electron 侧 Host 适配器（薄）。
//
// 平台无关核心、选择器档案、媒体格式化都在 repo 根 shared/inject/（单一事实来源，
// 与浏览器扩展 content script 共用同一份逻辑）。本文件只负责：
//   ①用 ipcRenderer 实现 host 接口（→ 主进程 → 本仓库 FastAPI 后端，规避 webview 跨域/CSP）；
//   ②把宿主下行事件（set-persona / set-reply-lang / set-account / fill-composer）桥接成回调；
//   ③启动共享核心。
//
// preload 在隔离世界运行但可用 require（main.js 已对 webview 关 sandbox）；
// 故直接 require repo 根 shared/inject（本仓库无 electron-builder 打包，目录结构稳定）。

const { ipcRenderer } = require("electron");

const profiles = require("../../shared/inject/profiles.js");
let mediaFormat = null;
try {
  mediaFormat = require("../../shared/inject/media-format.js");
} catch (e) {
  /* 媒体格式化模块缺失：媒体翻译降级关闭，不影响文本链路 */
}
const { createInject } = require("../../shared/inject/core.js");

const host = {
  translate: (args) => ipcRenderer.invoke("desktop:translate", args),
  translateMedia: (args) => ipcRenderer.invoke("desktop:translate-media", args),
  smartReply: (args) => ipcRenderer.invoke("desktop:smart-reply", args),
  ingest: (args) => ipcRenderer.invoke("desktop:ingest", args),
  getConfig: () => ipcRenderer.invoke("desktop:config"),
  getSelectorProfiles: () => ipcRenderer.invoke("desktop:selector-profiles"),
  diag: (msg) => {
    try {
      ipcRenderer.invoke("desktop:diag", msg);
    } catch (e) {
      /* 忽略 */
    }
  },
  injectHealth: (payload) => {
    try {
      ipcRenderer.invoke("desktop:inject-health", payload);
    } catch (e) {
      /* 后端不可达：下次心跳重试 */
    }
  },
  // 桌面壳顶栏注入状态条：经 webview→host 通道上报
  reportInjectStatus: (payload) => {
    try {
      ipcRenderer.sendToHost("inject-status", payload);
    } catch (e) {
      /* 非 webview 宿主：忽略 */
    }
  },
  reportActiveChat: (payload) => {
    try {
      ipcRenderer.sendToHost("active-chat", payload);
    } catch (e) {
      /* 非 webview 宿主：忽略 */
    }
  },
  onSetPersona: (cb) => ipcRenderer.on("set-persona", (_e, payload) => cb(payload)),
  onSetReplyLang: (cb) => ipcRenderer.on("set-reply-lang", (_e, payload) => cb(payload)),
  onSetAccount: (cb) => ipcRenderer.on("set-account", (_e, payload) => cb(payload)),
  onFillComposer: (cb) => ipcRenderer.on("fill-composer", (_e, payload) => cb(payload)),
};

createInject(host, { profiles, mediaFormat }).autostart();
