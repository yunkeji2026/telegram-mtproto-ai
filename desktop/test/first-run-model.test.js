"use strict";

// 首启向导纯函数模型单测（P0-1 A2/A5，无框架，node 直跑）：node test/first-run-model.test.js
const assert = require("assert");
const {
  FR_STRINGS,
  frT,
  frBuildSteps,
  frAiPrefill,
  frValidateAiInput,
  frAiTestView,
  frAiSaveView,
  frResultView,
} = require("../renderer/first-run-model.js");

let pass = 0;
function ok(name, cond) {
  assert.ok(cond, name);
  pass++;
}

// ── 步骤编排：AI 已配置 → 只走基础步；未配置/后端不可达 → 三步 ──
ok("已配置只走基础步", frBuildSteps({ ok: true, configured: true }).join(",") === "basic");
ok("未配置走三步", frBuildSteps({ ok: true, configured: false }).join(",") === "basic,ai,result");
ok("后端不可达也给填 Key 机会", frBuildSteps(null).join(",") === "basic,ai,result");
ok("状态接口失败同上", frBuildSteps({ ok: false }).join(",") === "basic,ai,result");

// ── 预填：状态可用给回显值；不可用回落桌面种子默认（deepseek） ──
const pre1 = frAiPrefill({ ok: true, configured: false, base_url: "http://x/v1", model: "m1" });
ok("预填用后端回显", pre1.base_url === "http://x/v1" && pre1.model === "m1");
const pre2 = frAiPrefill(null);
ok("后端不可达回落默认 base_url", pre2.base_url === "https://api.deepseek.com");
ok("后端不可达回落默认 model", pre2.model === "deepseek-chat");
const pre3 = frAiPrefill({ ok: true, configured: true, api_key_masked: "sk-a…mnop" });
ok("已配置带打码 key", pre3.configured === true && pre3.api_key_masked === "sk-a…mnop");

// ── 输入校验：key 必填；base/model 可空 ──
ok("空 key 拒绝", frValidateAiInput({ api_key: "  " }).ok === false);
ok("空 key 错误码", frValidateAiInput({ api_key: "" }).err === "key_required");
ok("有 key 放行", frValidateAiInput({ api_key: "sk-1" }).ok === true);
ok("base/model 可空", frValidateAiInput({ api_key: "sk-1", base_url: "", model: "" }).ok === true);

// ── 测试连接视图 ──
ok("测试成功绿字", frAiTestView({ ok: true }, "zh").cls === "ok");
const tf = frAiTestView({ ok: false, msg: "HTTP 401" }, "zh");
ok("测试失败带原因", tf.cls === "err" && tf.text.indexOf("HTTP 401") >= 0);
ok("测试无响应也 err", frAiTestView(null, "zh").cls === "err");

// ── 保存视图（A5 绿灯语义：ai_ready 才 ready） ──
const sv1 = frAiSaveView({ ok: true, ai_ready: true }, "zh");
ok("保存+就绪 → ok/ready", sv1.cls === "ok" && sv1.ready === true);
const sv2 = frAiSaveView({ ok: true, ai_ready: false }, "zh");
ok("保存但未验证 → warn/未就绪", sv2.cls === "warn" && sv2.ready === false);
const sv3 = frAiSaveView({ ok: false, detail: "保存失败：x" }, "zh");
ok("保存失败 → err 带 detail", sv3.cls === "err" && sv3.text.indexOf("保存失败：x") >= 0);
ok("无响应 → err", frAiSaveView(null, "zh").cls === "err");

// ── 结果终屏 ──
const r1 = frResultView(sv1, "zh");
ok("就绪 → 绿灯终屏", r1.cls === "ok" && r1.title === FR_STRINGS.zh.result_ok_title);
const r2 = frResultView(sv2, "zh");
ok("未就绪 → 黄灯终屏", r2.cls === "warn" && r2.title === FR_STRINGS.zh.result_warn_title);
ok("null 保存视图安全", frResultView(null, "zh").cls === "warn");

// ── i18n：zh/en 键齐 + 回落 ──
const zhKeys = Object.keys(FR_STRINGS.zh).sort().join("|");
const enKeys = Object.keys(FR_STRINGS.en).sort().join("|");
ok("zh/en 键集合一致", zhKeys === enKeys);
ok("en 取英文", frT("en", "btn_finish") === FR_STRINGS.en.btn_finish);
ok("未知语言回落 zh", frT("fr", "btn_finish") === FR_STRINGS.zh.btn_finish);
ok("未知键回显键名", frT("zh", "nope_x") === "nope_x");

console.log(`first-run-model.test.js: ${pass} passed`);
