"use strict";

// 注入诊断纯函数：把 inject(tg-inject.js) 上报的原始状态映射为「状态条」展示模型。
// 双模式：浏览器经 <script> 加载后成为全局函数（renderer.js 直接用）；
//        Node 经 require 取 module.exports（desktop/test/inject-status.test.js 单测）。
//
// 输入 s（inject-status 上报）：{ supported:bool, composer:bool, bubbles:int, chatOpen:bool }
// 输出：{ cls: "ok"|"warn"|"bad"|"wait", text, detail }
function deriveInjectState(s) {
  if (!s) return { cls: "wait", text: "等待注入…", detail: "注入脚本尚未上报状态" };
  if (!s.supported) return { cls: "bad", text: "无注入档案", detail: "该平台无选择器档案，功能不可用" };
  if (!s.chatOpen && !s.composer) {
    return { cls: "warn", text: "未登录/未进入会话", detail: "未检测到会话或输入框：请扫码登录并打开一个对话" };
  }
  if (!s.composer) {
    return { cls: "warn", text: "选择器失配（输入框）", detail: "找不到输入框，注入可能因官方改版失效（需校准 PROFILES.composer）" };
  }
  if (s.chatOpen && !s.bubbles) {
    return { cls: "warn", text: "选择器失配（消息）", detail: "会话已打开但抓不到消息气泡（需校准 PROFILES.bubble/text）" };
  }
  return { cls: "ok", text: "注入正常", detail: "输入框 ✓　消息气泡 ×" + (s.bubbles || 0) };
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { deriveInjectState };
}
