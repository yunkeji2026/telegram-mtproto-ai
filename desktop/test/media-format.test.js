"use strict";

// 媒体翻译结果格式化纯函数单测（无框架，node 直跑）：node test/media-format.test.js
const assert = require("assert");
const { formatMediaResult, pickTranslated } = require("../../shared/inject/media-format.js");

let pass = 0;
function ok(name, cond) {
  assert.ok(cond, name);
  pass++;
}

// ── pickTranslated：多字段兜底 ─────────────────────────────────────────
ok("translated_text 优先", pickTranslated({ translated_text: "a", text: "b" }) === "a");
ok("回退 text", pickTranslated({ text: "b" }) === "b");
ok("回退 translated", pickTranslated({ translated: "c" }) === "c");
ok("空对象 → 空串", pickTranslated({}) === "");

// ── 图片成功 ───────────────────────────────────────────────────────────
let f = formatMediaResult("image", {
  ok: true, ocr_text: "Hello", translation: { translated_text: "你好" },
});
ok("图片 ok", f.ok === true);
ok("图片原文", f.original === "Hello");
ok("图片译文", f.translated === "你好");
ok("图片 label", f.label === "图片");

// ── 语音成功 ───────────────────────────────────────────────────────────
f = formatMediaResult("voice", {
  ok: true, transcript: "how are you", translation: { translated_text: "你好吗" },
});
ok("语音 ok", f.ok && f.original === "how are you" && f.translated === "你好吗" && f.label === "语音");

// ── 失败：后端 message 优先 ────────────────────────────────────────────
f = formatMediaResult("image", { ok: false, reason: "vision_disabled", message: "图像识别未启用" });
ok("失败带 message", f.ok === false && f.note === "图像识别未启用");

// ── 失败：无 message 时 reason → 中文 ──────────────────────────────────
f = formatMediaResult("voice", { ok: false, reason: "asr_disabled" });
ok("reason 映射中文", f.ok === false && f.note.indexOf("语音转写未启用") >= 0);

// ── 失败：未知 reason 回退原值 ─────────────────────────────────────────
f = formatMediaResult("image", { ok: false, reason: "weird" });
ok("未知 reason 回退", f.ok === false && f.note === "weird");

// ── 健壮：null / 缺字段 ────────────────────────────────────────────────
ok("null 响应 → 失败", formatMediaResult("image", null).ok === false);
f = formatMediaResult("image", { ok: true, translation: {} });
ok("ok 但无内容 → 视为失败", f.ok === false && f.note.indexOf("未识别") >= 0);

console.log(`media-format.test.js: ${pass} passed`);
