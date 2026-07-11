"use strict";

// 首启向导纯函数模型（P0-1 A2/A5）：步骤编排 / AI 状态预填 / 测试与保存结果视图。
// 双模式：浏览器经 <script> 加载后挂全局（first-run.js 直接用）；
//        Node 经 require 取 module.exports（desktop/test/first-run-model.test.js 单测）。
//
// 设计：不碰 DOM / IPC——所有 I/O 由 first-run.js 完成，这里只做「响应 → 展示模型」映射，
// 保证向导流程可在无 Electron 环境下回归。

var FR_STRINGS = {
  zh: {
    ai_title: "配置 AI 大模型（翻译 / 智能回复引擎）",
    ai_sub: "只需一个 OpenAI 兼容 API Key（如 DeepSeek）。跳过则翻译暂不可用，之后可在后台「接入向导」补配。",
    ai_key_label: "AI API Key",
    ai_base_label: "接口地址（base_url）",
    ai_model_label: "模型",
    btn_test: "测试连接",
    btn_save: "保存并继续",
    btn_skip: "跳过",
    btn_back: "上一步",
    btn_finish: "完成并进入",
    testing: "正在连接 AI 服务…",
    test_ok: "连接成功，Key 有效",
    test_fail: "连接失败",
    saving: "正在保存…",
    save_fail: "保存失败",
    ready: "翻译就绪 ✓ 已保存并即时生效",
    saved_not_ready: "已保存，但连接自检未通过：请核对 Key / 接口地址 / 网络，稍后可在后台「接入向导」重试",
    key_required: "API Key 不能为空",
    backend_wait: "本地后台服务还在启动，请稍候几秒再试",
    result_ok_title: "一切就绪",
    result_ok_sub: "AI 已连通，翻译与智能回复可直接使用。",
    result_warn_title: "还差一步",
    result_warn_sub: "AI Key 已保存但未验证通过。进入后可在后台「接入向导 → AI 大模型」重测。",
    already_configured: "已检测到 AI 配置，无需重复填写。",
  },
  en: {
    ai_title: "Set up the AI model (translation / smart replies)",
    ai_sub: "One OpenAI-compatible API key (e.g. DeepSeek) is enough. Skip for now and translation stays off until configured in the admin setup wizard.",
    ai_key_label: "AI API Key",
    ai_base_label: "Endpoint (base_url)",
    ai_model_label: "Model",
    btn_test: "Test connection",
    btn_save: "Save & continue",
    btn_skip: "Skip",
    btn_back: "Back",
    btn_finish: "Finish",
    testing: "Connecting to AI service…",
    test_ok: "Connected — key works",
    test_fail: "Connection failed",
    saving: "Saving…",
    save_fail: "Save failed",
    ready: "Translation ready ✓ — saved and live",
    saved_not_ready: "Saved, but the connection check failed: verify key / endpoint / network. You can retest in the admin setup wizard.",
    key_required: "API Key is required",
    backend_wait: "Local backend is still starting — try again in a few seconds",
    result_ok_title: "All set",
    result_ok_sub: "AI is connected. Translation and smart replies work out of the box.",
    result_warn_title: "One more step",
    result_warn_sub: "The AI key is saved but not verified. Retest later in admin → setup wizard → AI model.",
    already_configured: "An AI configuration was detected — nothing to fill in.",
  },
};

function frT(lang, key) {
  var d = FR_STRINGS[lang === "en" ? "en" : "zh"] || FR_STRINGS.zh;
  return d[key] != null ? d[key] : key;
}

// 步骤编排：AI 已配置（升级/重装保留数据目录）→ 只走基础步；未配置 → 基础 + AI + 结果。
// aiStatus = GET /api/setup/ai 响应（或 null/失败 → 视为未配置，让用户有机会填）。
function frBuildSteps(aiStatus) {
  var configured = !!(aiStatus && aiStatus.ok && aiStatus.configured);
  return configured ? ["basic"] : ["basic", "ai", "result"];
}

// AI 步预填：后端不可达/未配置时给桌面种子同款默认（deepseek），保证表单可直接填。
function frAiPrefill(aiStatus) {
  var st = (aiStatus && aiStatus.ok) ? aiStatus : {};
  return {
    base_url: st.base_url || "https://api.deepseek.com",
    model: st.model || "deepseek-chat",
    configured: !!st.configured,
    api_key_masked: st.api_key_masked || "",
  };
}

// 输入校验（提交前）：key 必填；base_url/model 可空（后端回落已存值/默认）。
function frValidateAiInput(vals) {
  var key = String((vals && vals.api_key) || "").trim();
  if (!key) return { ok: false, err: "key_required" };
  return { ok: true, err: "" };
}

// POST /api/setup/test-ai 响应 → 展示模型 {cls, text}
function frAiTestView(resp, lang) {
  if (resp && resp.ok) return { cls: "ok", text: frT(lang, "test_ok") };
  var extra = resp && (resp.msg || resp.detail) ? String(resp.msg || resp.detail) : "";
  return { cls: "err", text: frT(lang, "test_fail") + (extra ? ": " + extra : "") };
}

// POST /api/setup/ai-key 响应 → 展示模型 {cls, ready, text}
// ready=true 才算「翻译就绪」绿灯（后端已用新 key 实连自检）。
function frAiSaveView(resp, lang) {
  if (!resp || !resp.ok) {
    var extra = resp && resp.detail ? String(resp.detail) : "";
    return { cls: "err", ready: false, text: frT(lang, "save_fail") + (extra ? ": " + extra : "") };
  }
  if (resp.ai_ready) return { cls: "ok", ready: true, text: frT(lang, "ready") };
  return { cls: "warn", ready: false, text: frT(lang, "saved_not_ready") };
}

// 结果步（A5 绿灯）：保存视图 → 终屏标题/副文案
function frResultView(saveView, lang) {
  if (saveView && saveView.ready) {
    return { cls: "ok", title: frT(lang, "result_ok_title"), sub: frT(lang, "result_ok_sub") };
  }
  return { cls: "warn", title: frT(lang, "result_warn_title"), sub: frT(lang, "result_warn_sub") };
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    FR_STRINGS: FR_STRINGS,
    frT: frT,
    frBuildSteps: frBuildSteps,
    frAiPrefill: frAiPrefill,
    frValidateAiInput: frValidateAiInput,
    frAiTestView: frAiTestView,
    frAiSaveView: frAiSaveView,
    frResultView: frResultView,
  };
}
