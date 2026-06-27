"use strict";

// 🩺 健康看板纯函数单测（无框架，node 直跑）：node test/health-panel.test.js
const assert = require("assert");
const {
  injectBadge, outboundBadge, formatDuration, injectStatusText,
  accountRowModel, renderPanelHtml, alertDotModel, renderAlertsHtml,
  formatValidateResult, outboundStatusText, outboundActions,
  outboundRowModel, renderOutboundRowHtml,
  interceptRateModel, renderReviewHtml, correctionsModel,
  renderInlineEditHtml, editSavePayload, slaBreachModel,
  reasonLabel, renderInterceptChipsHtml, reasonClusterModel,
  selectorDiagnosisModel,
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
ok("outbound held→warn", outboundBadge({ held: 3 }).cls === "warn");
ok("outbound held 文案", outboundBadge({ held: 3 }).text.indexOf("3") >= 0);
ok("outbound failed 优先 held", outboundBadge({ failed: 1, held: 2 }).cls === "bad");

// ── P2 出站人审：状态文案 / 动作矩阵 / 行模型 / 行渲染 ─────────────────────────
ok("ob status pending", outboundStatusText("pending") === "待发");
ok("ob status held", outboundStatusText("held") === "待审核");
ok("ob status cancelled", outboundStatusText("cancelled") === "已拦截");
ok("ob status 未知透传", outboundStatusText("weird") === "weird");

ok("ob actions pending=3", outboundActions("pending").length === 3);
ok("ob actions pending 含拦截", outboundActions("pending").some((a) => a.act === "cancel"));
ok("ob actions held 含放行", outboundActions("held").some((a) => a.act === "release"));
ok("ob actions failed=retry", outboundActions("failed").length === 1 && outboundActions("failed")[0].act === "retry");
ok("ob actions claimed=空", outboundActions("claimed").length === 0);
ok("ob actions sent=空", outboundActions("sent").length === 0);

const _obm = outboundRowModel({ id: 7, status: "held", account_id: "ig1", preview: "你好" });
ok("ob rowModel id", _obm.id === 7);
ok("ob rowModel statusText", _obm.statusText === "待审核");
ok("ob rowModel actions", _obm.actions.length === 3);
ok("ob rowModel preview 回退 text", outboundRowModel({ status: "pending", text: "原文" }).preview === "原文");

const _heldHtml = renderOutboundRowHtml({ id: 9, status: "held", account_id: "ig1", preview: "待审" });
ok("ob render held 带 data-oid", _heldHtml.indexOf('data-oid="9"') >= 0);
ok("ob render held 放行按钮", _heldHtml.indexOf('data-act="release"') >= 0 && _heldHtml.indexOf('data-id="9"') >= 0);
ok("ob render held 改写按钮", _heldHtml.indexOf('data-act="edit"') >= 0);
const _sentHtml = renderOutboundRowHtml({ id: 1, status: "sent", account_id: "ig1", preview: "x" });
ok("ob render sent 无动作按钮", _sentHtml.indexOf("data-act=") < 0);
const _failHtml = renderOutboundRowHtml({ id: 2, status: "failed", account_id: "ig1", preview: "x" });
ok("ob render failed 重试按钮", _failHtml.indexOf('data-act="retry"') >= 0);
ok("ob render 转义 preview", renderOutboundRowHtml({ id: 3, status: "pending", preview: "<b>" }).indexOf("<b>") < 0);

// ── P3 拦截率读数 ────────────────────────────────────────────────────────────
ok("rate 样本不足→—", interceptRateModel({ intercept_rate: 0, intercept_sample: 0 }).pct === "—");
ok("rate 低→ok", interceptRateModel({ intercept_rate: 0.1, intercept_sample: 10 }).cls === "ok");
ok("rate 中→warn", interceptRateModel({ intercept_rate: 0.3, intercept_sample: 10 }).cls === "warn");
ok("rate 高→bad", interceptRateModel({ intercept_rate: 0.7, intercept_sample: 10 }).cls === "bad");
ok("rate 百分比", interceptRateModel({ intercept_rate: 0.25, intercept_sample: 8 }).pct === "25%");
ok("rate null 安全", interceptRateModel(null).pct === "—");

// ── P3 待审队列块 ────────────────────────────────────────────────────────────
ok("review 空→空串", renderReviewHtml([]) === "");
ok("review null→空串", renderReviewHtml(null) === "");
const _rev = renderReviewHtml([
  { id: 11, account_id: "ig1", preview: "甲" },
  { id: 12, account_id: "ig2", preview: "乙" },
]);
ok("review 计数", _rev.indexOf("待审 2 条") >= 0);
ok("review 全部放行", _rev.indexOf('data-bulk="release"') >= 0 && _rev.indexOf('data-ids="11,12"') >= 0);
ok("review 全部拦截", _rev.indexOf('data-bulk="cancel"') >= 0);
ok("review 行带 held 动作", _rev.indexOf('data-act="release"') >= 0 && _rev.indexOf('data-oid="11"') >= 0);
ok("review 强制 held 即便缺 status", renderReviewHtml([{ id: 5, preview: "x" }]).indexOf('data-act="release"') >= 0);

// ── P3 面板整合：待审块 + 拦截率出现在受控出站区 ──────────────────────────────
const _panelP3 = renderPanelHtml(
  { summary: { total: 1 }, accounts: [] },
  { summary: { held: 2, sent: 8, cancelled: 2 }, recent: [], review: [{ id: 1, account_id: "ig1", preview: "审" }], intercept_rate: 0.2, intercept_sample: 10 },
  null,
);
ok("panel 含待审块", _panelP3.indexOf("待审 1 条") >= 0);
ok("panel 含拦截率", _panelP3.indexOf("拦截率") >= 0);

// ── P4.2 纠正样本读数 ────────────────────────────────────────────────────────
ok("corr 无样本→不显示", correctionsModel({ corrections: { total: 0 } }).has === false);
ok("corr null 安全", correctionsModel(null).has === false);
ok("corr 有样本", correctionsModel({ corrections: { edit: 3, cancel: 1, total: 4 } }).has === true);
ok("corr 文案含总数", correctionsModel({ corrections: { edit: 3, total: 4 } }).text.indexOf("4") >= 0);
ok("corr 文案含改写数", correctionsModel({ corrections: { edit: 3, total: 4 } }).text.indexOf("改写 3") >= 0);
ok("corr 无 AI 协同不显示", correctionsModel({ corrections: { edit: 3, total: 3, ai_assisted: 0 } }).text.indexOf("AI 协同") < 0);
ok("corr 有 AI 协同显示", correctionsModel({ corrections: { edit: 3, total: 3, ai_assisted: 2 } }).text.indexOf("AI 协同 2") >= 0);

// ── P4.4 改写保存载荷：source 推断 ────────────────────────────────────────────
ok("save 纯人改→human", editSavePayload(1, "我写的", "").source === "human");
ok("save AI 原样采纳→ai_adopted", editSavePayload(1, "候选", "候选").source === "ai_adopted");
ok("save AI 采纳带空白→ai_adopted", editSavePayload(1, " 候选 ", "候选").source === "ai_adopted");
ok("save AI 微调→ai_edited", editSavePayload(1, "候选+改", "候选").source === "ai_edited");
ok("save 带 ai_suggestion 字段", editSavePayload(1, "x", "候选").ai_suggestion === "候选");
ok("save action 恒为 edit", editSavePayload(1, "x", "").action === "edit");
ok("save 透传 id/text", editSavePayload(7, "正文", "").id === 7 && editSavePayload(7, "正文", "").text === "正文");
const _panelCorr = renderPanelHtml(
  { summary: { total: 1 }, accounts: [] },
  { summary: {}, recent: [], review: [], corrections: { edit: 2, total: 2 } },
  null,
);
ok("panel 含纠正样本读数", _panelCorr.indexOf("纠正样本 2 条") >= 0);
ok("panel 含导出样本按钮", _panelCorr.indexOf('id="cp-corr-export"') >= 0);
const _panelNoCorr = renderPanelHtml(
  { summary: { total: 1 }, accounts: [] },
  { summary: {}, recent: [], review: [], corrections: { total: 0 } },
  null,
);
ok("无样本不显示导出按钮", _panelNoCorr.indexOf('id="cp-corr-export"') < 0);

// ── P7 结构化拦截理由 + 失误聚类 ──────────────────────────────────────────────
ok("reasonLabel 已知码", reasonLabel("off_topic") === "答非所问");
ok("reasonLabel 未知码透传", reasonLabel("weird") === "weird");
const _chips = renderInterceptChipsHtml(8);
ok("chips 含答非所问", _chips.indexOf('data-cancel-reason="off_topic"') >= 0 && _chips.indexOf('data-id="8"') >= 0);
ok("chips 含事实错误", _chips.indexOf('data-cancel-reason="factual"') >= 0);
ok("chips 含取消", _chips.indexOf("data-cancel-abort") >= 0);
ok("cluster 空→不显示", reasonClusterModel({ reason_clusters: {} }).has === false);
ok("cluster null 安全", reasonClusterModel(null).has === false);
const _cl = reasonClusterModel({ reason_clusters: { off_topic: 3, factual: 5 } });
ok("cluster 有数据", _cl.has === true);
ok("cluster 降序（事实错误在前）", _cl.entries[0].label === "事实错误" && _cl.entries[0].count === 5);
ok("cluster 文案中文化", _cl.text.indexOf("答非所问 3") >= 0 && _cl.text.indexOf("事实错误 5") >= 0);
ok("cluster 未知码透传 label", reasonClusterModel({ reason_clusters: { custom: 2 } }).entries[0].label === "custom");
const _panelCluster = renderPanelHtml(
  { summary: { total: 1 }, accounts: [] },
  { summary: {}, recent: [], review: [], reason_clusters: { off_topic: 2 } },
  null,
);
ok("panel 含失误聚类", _panelCluster.indexOf("失误聚类") >= 0 && _panelCluster.indexOf("答非所问 2") >= 0);

// ── P4.1 行内编辑器（含 AI 重写） ────────────────────────────────────────────
const _inline = renderInlineEditHtml(42);
ok("inline 含输入框", _inline.indexOf('class="cp-ob-edit"') >= 0);
ok("inline 含 AI 重写", _inline.indexOf('data-edit-airewrite="42"') >= 0);
ok("inline 含保存", _inline.indexOf('data-edit-save="42"') >= 0);
ok("inline 含取消", _inline.indexOf('data-edit-cancel="1"') >= 0);

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

// ── P4.3 人审 SLA 超时 ────────────────────────────────────────────────────────
ok("sla 无待审→不告警", slaBreachModel({ summary: { held: 0 }, review_oldest_age_sec: 999 }, 300).breach === false);
ok("sla 未超阈值→不告警", slaBreachModel({ summary: { held: 2 }, review_oldest_age_sec: 100 }, 300).breach === false);
ok("sla 超阈值→告警", slaBreachModel({ summary: { held: 2 }, review_oldest_age_sec: 400 }, 300).breach === true);
ok("sla 告警带 count", slaBreachModel({ summary: { held: 3 }, review_oldest_age_sec: 400 }, 300).count === 3);
ok("sla 边界等于阈值→告警", slaBreachModel({ summary: { held: 1 }, review_oldest_age_sec: 300 }, 300).breach === true);
ok("sla null 安全", slaBreachModel(null, 300).breach === false);
// alertDotModel 第三参 sla：注入失配优先，否则 SLA 触发
ok("dot 注入优先于 SLA", alertDotModel(null, { alerts: [{}, {}] }, { breach: true, count: 5 }).count === 2);
ok("dot 无注入时 SLA 点亮", alertDotModel(null, { alerts: [] }, { breach: true, count: 4 }).on === true);
ok("dot SLA 点亮带 count", alertDotModel(null, { alerts: [] }, { breach: true, count: 4 }).count === 4);
ok("dot SLA title 含待审超时", alertDotModel(null, { alerts: [] }, { breach: true, count: 4 }).title.indexOf("待审超时") >= 0);
ok("dot 无 SLA 参数兼容", alertDotModel(null, { alerts: [] }).on === false);

// ── P6 SLA 分级 + 阈值可配 ────────────────────────────────────────────────────
ok("sla level none（未超）", slaBreachModel({ summary: { held: 1 }, review_oldest_age_sec: 50 }, 300).level === "none");
ok("sla level warn", slaBreachModel({ summary: { held: 1 }, review_oldest_age_sec: 400 }, 300).level === "warn");
ok("sla level urgent（默认 urgent=warn*3）", slaBreachModel({ summary: { held: 1 }, review_oldest_age_sec: 1000 }, 300).level === "urgent");
ok("sla urgent 标记", slaBreachModel({ summary: { held: 1 }, review_oldest_age_sec: 1000 }, 300).urgent === true);
// payload 配置优先于入参
ok("sla payload warn 优先", slaBreachModel({ summary: { held: 1 }, review_oldest_age_sec: 130, review_sla_sec: 120, review_sla_urgent_sec: 600 }, 300).level === "warn");
ok("sla payload urgent 优先", slaBreachModel({ summary: { held: 1 }, review_oldest_age_sec: 650, review_sla_sec: 120, review_sla_urgent_sec: 600 }, 300).level === "urgent");
ok("sla urgent 边界等于阈值", slaBreachModel({ summary: { held: 1 }, review_oldest_age_sec: 600, review_sla_sec: 120, review_sla_urgent_sec: 600 }, 300).level === "urgent");
// dot 携带 level
ok("dot SLA urgent level", alertDotModel(null, { alerts: [] }, { breach: true, level: "urgent", count: 2 }).level === "urgent");
ok("dot SLA urgent title 含严重", alertDotModel(null, { alerts: [] }, { breach: true, level: "urgent", count: 2 }).title.indexOf("严重") >= 0);
ok("dot 注入 level=mismatch", alertDotModel(null, { alerts: [{}] }).level === "mismatch");

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
ok("alerts 含一键热修按钮", ah.indexOf("cp-health-fix") >= 0 && ah.indexOf("热修选择器") >= 0);
ok("alerts 无告警时无热修按钮", renderAlertsHtml({ alerts: [] }).indexOf("cp-health-fix") < 0);

// ── P9 逐选择器失配诊断（selectorDiagnosisModel + renderAlertsHtml 内联）─────────
ok("diag 空→has=false", selectorDiagnosisModel({}).has === false);
ok("diag null→has=false", selectorDiagnosisModel(null).has === false);
// 后端聚合 selector_diagnosis 优先
const diagSrv = selectorDiagnosisModel({ selector_diagnosis: [
  { key: "sendBtn", missing: 3 }, { key: "composer", missing: 1 },
] });
ok("diag 用后端聚合", diagSrv.has === true && diagSrv.entries[0].key === "sendBtn");
ok("diag 含人话标签", diagSrv.entries[0].label === "发送按钮");
ok("diag 文案含定位", diagSrv.text.indexOf("失配定位") >= 0 && diagSrv.text.indexOf("发送按钮 ✗3") >= 0);
// 无后端聚合 → 从 alerts[].selectors 客户端兜底统计 + 降序
const diagCli = selectorDiagnosisModel({ alerts: [
  { selectors: { composer: true, sendBtn: false, bubble: false, peerTitle: true } },
  { selectors: { composer: false, sendBtn: false, bubble: true, peerTitle: true } },
] });
ok("diag 客户端兜底统计", diagCli.has === true && diagCli.entries[0].key === "sendBtn" && diagCli.entries[0].missing === 2);
ok("diag 兜底忽略命中键", diagCli.entries.every((e) => e.missing > 0));
// renderAlertsHtml 内联诊断行
const ahDiag = renderAlertsHtml({
  alerts: [{ platform: "telegram", account_id: "a1", status: "mismatch_composer", mismatch_secs: 320,
             selectors: { composer: false, sendBtn: false, bubble: true, peerTitle: true } }],
});
ok("alerts 内联失配定位", ahDiag.indexOf("失配定位") >= 0);
ok("alerts 无 selectors 时不渲染诊断", renderAlertsHtml({ alerts: [
  { platform: "telegram", account_id: "a1", status: "mismatch_composer", mismatch_secs: 320 },
] }).indexOf("失配定位") < 0);

// ── renderPanelHtml 第三参 alertsData：顶部渲染红框 ───────────────────────────
const htmlWithAlert = renderPanelHtml(
  { summary: { total: 1, mismatch: 1, persistent_mismatch: 1 }, accounts: [] },
  { summary: {}, recent: [] },
  { alerts: [{ platform: "wa", account_id: "z", status: "mismatch_bubble", mismatch_secs: 100 }] },
);
ok("panel 含告警红框", htmlWithAlert.indexOf("注入持续失配") >= 0);
ok("panel 无第三参时无红框", renderPanelHtml({ summary: {} }, { summary: {} }).indexOf("注入持续失配") < 0);

// ── 工具行（校验/重载）常驻面板 ──────────────────────────────────────────────
const toolsHtml = renderPanelHtml({ summary: {} }, { summary: {} });
ok("panel 含校验按钮", toolsHtml.indexOf("cp-health-validate") >= 0 && toolsHtml.indexOf("校验覆写") >= 0);
ok("panel 含重载按钮", toolsHtml.indexOf("cp-health-reload") >= 0 && toolsHtml.indexOf("重载注入") >= 0);
ok("panel 含工具消息位", toolsHtml.indexOf("cp-health-tools-msg") >= 0);

// ── formatValidateResult ─────────────────────────────────────────────────────
ok("validate 后端不可达→bad", formatValidateResult(null).cls === "bad");
ok("validate ok:false→bad", formatValidateResult({ ok: false, error: "x" }).cls === "bad");
ok("validate 文件不存在→wait", formatValidateResult({ ok: true, exists: false, valid: true }).cls === "wait");
ok("validate JSON 无效→bad", formatValidateResult({ ok: true, exists: true, valid: false, error: "逗号" }).cls === "bad");
ok("validate JSON 无效含 error 文案", formatValidateResult({ ok: true, exists: true, valid: false, error: "逗号" }).text.indexOf("逗号") >= 0);
const vok = formatValidateResult({ ok: true, exists: true, valid: true, profiles: 2, dropped: [] });
ok("validate 全有效→ok", vok.cls === "ok" && vok.text.indexOf("2 个平台") >= 0);
const vwarn = formatValidateResult({ ok: true, exists: true, valid: true, profiles: 1, dropped: ["telegram.nope", "wa.bad"] });
ok("validate 有忽略→warn", vwarn.cls === "warn");
ok("validate 忽略计数+列举", vwarn.text.indexOf("已忽略 2 项") >= 0 && vwarn.text.indexOf("telegram.nope") >= 0);

console.log(`health-panel.test.js: ${pass} passed`);
