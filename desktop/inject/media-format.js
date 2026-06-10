"use strict";

// 媒体翻译结果格式化（纯函数）：把后端 translate-image / translate-voice 的响应
// 归一成 {ok, label, original, translated, note} 供注入气泡渲染。
// 双模式：webview preload(tg-inject.js) require 本文件；Node 单测亦 require（无 DOM/electron 依赖）。

function pickTranslated(translation) {
  const t = translation || {};
  return t.translated_text || t.text || t.translated || "";
}

// kind: "image" | "voice"；res: 后端 JSON（或 {ok:false,...}）。
function formatMediaResult(kind, res) {
  const label = kind === "image" ? "图片" : "语音";
  if (!res || res.ok === false || res.ok == null) {
    const note =
      (res && (res.message || REASON_TEXT[res.reason])) ||
      (res && res.reason) ||
      "翻译失败";
    return { ok: false, label: label, original: "", translated: "", note: note };
  }
  const original = kind === "image" ? res.ocr_text || "" : res.transcript || "";
  const translated = pickTranslated(res.translation);
  if (!translated && !original) {
    return { ok: false, label: label, original: "", translated: "", note: "未识别到可翻译内容" };
  }
  return { ok: true, label: label, original: original, translated: translated, note: "" };
}

// 后端常见 reason → 中文提示（便于坐席判断是"未启用"还是"无后端"还是"识别空"）
const REASON_TEXT = {
  vision_disabled: "图像识别未启用（config.vision.enabled）",
  no_vision_backend: "未配置可用的图像识别后端",
  ocr_error: "图像识别出错",
  no_text: "图片中未识别到文字",
  asr_disabled: "语音转写未启用（config.audio_pipeline.enabled）",
  asr_error: "语音转写出错",
  asr_failed: "语音转写失败",
  no_speech: "未识别到语音内容",
};

if (typeof module !== "undefined" && module.exports) {
  module.exports = { formatMediaResult, pickTranslated, REASON_TEXT };
}
