"use strict";

// 🩺 健康看板纯函数单测（无框架，node 直跑）：node test/health-panel.test.js
const assert = require("assert");
const {
  injectBadge, outboundBadge, formatDuration, injectStatusText,
  accountRowModel, renderPanelHtml, alertDotModel, renderAlertsHtml,
} = require("../renderer/health-panel.js");

let pass = 0;
function ok(name, cond) {
  assert.ok(cond, name);
  pass++;
}

// ── injectBadge：严重度优先 bad>warn>wait>ok ─────────────────────────────────
ok("inject persistent→bad", injectBadge({ persistent_mismatch: 2, mismatch: 3, total: 5 }).cls === "bad");
ok("inject mismatch→warn", injectBadge({ persistent_mismatch: 0, mismatch: 1, total: 4 }).cls === "warn");
ok("inject empty→wait", injectBadge({ total: 0 }).cls === "wait");
ok("inject all ok", injectBadge({ total: 3, mismatch: 0, persistent_mismatch: 0 }).cls === "ok");
ok("inject ok 文案含总数", injectBadge({ total: 3 }).text.indexOf("3") >= 0);
ok("inject null 安全", injectBadge(null).cls === "wait");

// ── outboundBadge：failed>活动>空闲 ─────────────────────────────────────────
ok("outbound failed→bad", outboundBadge({ failed: 1, pending: 2 }).cls === "bad");
ok("outbound active→warn", outboundBadge({ pending: 2, claimed: 1 }).cls === "warn");
ok("outbound active 文案", outboundBadge({ pending: 2, claimed: 1 }).text.indexOf("待发 2") >= 0);
ok("outbound idle→ok", outboundBadge({ sent: 9 }).cls === "ok");
ok("outbound null 安全", outboundBadge(null).cls === "ok");

// ── formatDuration ──────────────────────────────────────────────────────────
ok("dur 秒", formatDuration(45) === "45 秒");
ok("dur 分秒", formatDuration(200) === "3 分 20 秒");
ok("dur 整分", formatDuration(180) === "3 分");
ok("dur 时分", formatDuration(3720) === "1 时 2 分");
ok("dur 负数→0秒", formatDuration(-5) === "0 秒");
ok("dur NaN→0秒", formatDuration("x") === "0 秒");

// ── injectStatusText 对齐 deriveInjectState 语义 ─────────────────────────────
ok("status ok", injectStatusText("ok") === "注入正常");
ok("status composer", injectStatusText("mismatch_composer").indexOf("输入框") >= 0);
ok("status bubble", injectStatusText("mismatch_bubble").indexOf("消息") >= 0);
ok("status unsupported", injectStatusText("unsupported") === "无注入档案");
ok("status 未知透传", injectStatusText("weird") === "weird");

// ── accountRowModel ─────────────────────────────────────────────────────────
const ok1 = accountRowModel({ platform: "telegram", account_id: "a1", status: "ok" });
ok("row ok cls", ok1.cls === "ok" && ok1.durationText === "");
const mm = accountRowModel({ platform: "wa", account_id: "b2", status: "mismatch_composer", mismatch_secs: 200 });
ok("row mismatch cls", mm.cls === "warn");
ok("row mismatch 持续时长", mm.durationText === "3 分 20 秒");
const stale = accountRowModel({ platform: "x", account_id: "c", status: "ok", stale: true });
ok("row stale cls", stale.cls === "stale" && stale.stale === true);
const uns = accountRowModel({ status: "unsupported" });
ok("row unsupported→bad", uns.cls === "bad");

// ── renderPanelHtml：纯渲染、可注入数据、含转义 ──────────────────────────────
const html = renderPanelHtml(
  { summary: { total: 2, mismatch: 1 }, accounts: [
    { platform: "telegram", account_id: "a1", status: "ok" },
    { platform: "wa", account_id: "<x>", status: "mismatch_bubble", mismatch_secs: 90 },
  ] },
  { summary: { pending: 1, failed: 0 }, recent: [
    { status: "sent", account_id: "a1", preview: "hi" },
  ] },
);
ok("html 含标题", html.indexOf("自动化健康") >= 0);
ok("html 含刷新按钮 id", html.indexOf("cp-health-refresh") >= 0);
ok("html 含账号", html.indexOf("telegram / a1") >= 0);
ok("html XSS 转义", html.indexOf("&lt;x&gt;") >= 0 && html.indexOf("<x>") < 0);
ok("html 含出站预览", html.indexOf("hi") >= 0);
ok("html 空数据不抛错", typeof renderPanelHtml({}, {}) === "string");
ok("html null 不抛错", typeof renderPanelHtml(null, null) === "string");

// ── alertDotModel：alerts 优先，退回 summary.persistent_mismatch ───────────────
ok("dot off 无告警", alertDotModel({ summary: {} }, { alerts: [] }).on === false);
ok("dot on by alerts", alertDotModel(null, { alerts: [{ account_id: "a" }, { account_id: "b" }] }).on === true);
ok("dot count by alerts", alertDotModel(null, { alerts: [{}, {}, {}] }).count === 3);
ok("dot 退回 summary", alertDotModel({ summary: { persistent_mismatch: 2 } }, null).count === 2);
ok("dot on 时 title 含提示", alertDotModel(null, { alerts: [{}] }).title.indexOf("持续失配") >= 0);
ok("dot off 时 title 默认", alertDotModel(null, { alerts: [] }).title.indexOf("自动化健康") >= 0);
ok("dot null 安全", alertDotModel(null, null).on === false);

// ── renderAlertsHtml ─────────────────────────────────────────────────────────
ok("alerts 空→空串", renderAlertsHtml({ alerts: [] }) === "");
ok("alerts null→空串", renderAlertsHtml(null) === "");
const ah = renderAlertsHtml({ alerts: [
  { platform: "telegram", account_id: "a1", status: "mismatch_composer", mismatch_secs: 320 },
] });
ok("alerts 红框标题", ah.indexOf("注入持续失配") >= 0);
ok("alerts 含账号", ah.indexOf("telegram / a1") >= 0);
ok("alerts 含持续时长", ah.indexOf("5 分 20 秒") >= 0);
ok("alerts 含热修指引", ah.indexOf("selector_profiles".replace("_", "-")) >= 0 || ah.indexOf("desktop_selector_profiles.json") >= 0);

// ── renderPanelHtml 第三参 alertsData：顶部渲染红框 ───────────────────────────
const htmlWithAlert = renderPanelHtml(
  { summary: { total: 1, mismatch: 1, persistent_mismatch: 1 }, accounts: [] },
  { summary: {}, recent: [] },
  { alerts: [{ platform: "wa", account_id: "z", status: "mismatch_bubble", mismatch_secs: 100 }] },
);
ok("panel 含告警红框", htmlWithAlert.indexOf("注入持续失配") >= 0);
ok("panel 无第三参时无红框", renderPanelHtml({ summary: {} }, { summary: {} }).indexOf("注入持续失配") < 0);

console.log(`health-panel.test.js: ${pass} passed`);
