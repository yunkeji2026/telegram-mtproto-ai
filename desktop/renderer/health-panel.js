"use strict";

// 🩺 自动化健康看板（桌面壳层）——把后端既有聚合 API 下发的「全账号注入健康 + 受控出站队列概览」
// 渲染到 copilot 头部 🩺 面板，让运营在桌面壳内一眼看清：
//   ① 各内嵌账号注入是否健康（持续失配=疑似官方网页改版，可走 D1 selector-profiles 热修）；
//   ② 全自动回复经 send-gate/kill-switch 后，受控出站队列是否正常流转 / 有无卡死。
// 数据来源：window.shell.injectHealthList() / outboundStats()（主进程代理后端，复用既有路由）。
//
// 双模式：浏览器经 <script> 加载执行 DOM 装配；Node 经 require 取纯函数（health-panel.test.js 单测）。
// 纯渲染模型与 inject-status.js::deriveInjectState 的语义对齐（同一套失配分类）。

function _num(obj, key) {
  const v = obj && obj[key];
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

// 注入健康总徽标：bad>warn>wait>ok 的严重度优先。
function injectBadge(summary) {
  const s = summary || {};
  const persistent = _num(s, "persistent_mismatch");
  const mismatch = _num(s, "mismatch");
  const total = _num(s, "total");
  if (persistent > 0) {
    return { cls: "bad", text: persistent + " 账号持续失配", hint: "疑似官方网页改版，可热修 selector-profiles(D1)" };
  }
  if (mismatch > 0) {
    return { cls: "warn", text: mismatch + " 账号选择器失配", hint: "短暂失配可自愈；持续则需校准" };
  }
  if (total === 0) {
    return { cls: "wait", text: "暂无注入账号", hint: "打开内嵌官方页并登录后开始上报" };
  }
  return { cls: "ok", text: "全部正常（" + total + "）", hint: "" };
}

// 受控出站队列徽标：failed > 待审(held) > 活动中(pending/claimed) > 空闲。
function outboundBadge(summary) {
  const s = summary || {};
  const failed = _num(s, "failed");
  const held = _num(s, "held");
  const pending = _num(s, "pending");
  const claimed = _num(s, "claimed");
  if (failed > 0) {
    return { cls: "bad", text: failed + " 条发送失败", hint: "DOM 发送未成功（已记 failed，非误判已送达）" };
  }
  if (held > 0) {
    return { cls: "warn", text: held + " 条待人工审核", hint: "review_mode：放行后才会自动发送" };
  }
  if (pending + claimed > 0) {
    return { cls: "warn", text: "活动中：待发 " + pending + " · 发送中 " + claimed, hint: "" };
  }
  return { cls: "ok", text: "队列空闲", hint: "" };
}

// 出站状态码 → 中文。
function outboundStatusText(status) {
  switch (status) {
    case "pending": return "待发";
    case "claimed": return "发送中";
    case "sent": return "已发送";
    case "failed": return "失败";
    case "held": return "待审核";
    case "cancelled": return "已拦截";
    default: return String(status || "");
  }
}

const _OB_COLOR = {
  pending: "#7f8c8d", claimed: "#f1c40f", sent: "#2ecc71",
  failed: "#e74c3c", held: "#f39c12", cancelled: "#7f8c8d",
};

// 按状态决定可用的人审动作（claimed=飞行中、sent/cancelled=终态 → 无动作）。
function outboundActions(status) {
  switch (status) {
    case "pending": return [{ act: "cancel", label: "拦截" }, { act: "hold", label: "暂停" }, { act: "edit", label: "改写" }];
    case "held": return [{ act: "release", label: "放行" }, { act: "cancel", label: "拦截" }, { act: "edit", label: "改写" }];
    case "failed": return [{ act: "retry", label: "重试" }];
    default: return [];
  }
}

// 单条出站命令 → 行展示模型（纯函数、可单测）。
function outboundRowModel(item) {
  const it = item || {};
  const status = String(it.status || "");
  return {
    id: it.id,
    status,
    statusText: outboundStatusText(status),
    actions: outboundActions(status),
    preview: String(it.preview != null ? it.preview : (it.text || "")),
    account_id: String(it.account_id || ""),
  };
}

function renderOutboundRowHtml(item) {
  const m = outboundRowModel(item);
  const color = _OB_COLOR[m.status] || "#7f8c8d";
  let btns = "";
  for (const a of m.actions) {
    btns += '<button data-act="' + a.act + '" data-id="' + _esc(m.id)
      + '" style="font-size:.66rem;padding:.05rem .35rem;border:1px solid #ffffff33;'
      + 'border-radius:5px;background:transparent;color:inherit;cursor:pointer;flex:0 0 auto">'
      + _esc(a.label) + "</button>";
  }
  return '<div data-oid="' + _esc(m.id) + '" style="display:flex;gap:.35rem;align-items:center;'
    + 'padding:.2rem 0;border-top:1px solid #ffffff14;font-size:.73rem">'
    + '<span style="color:' + color + ';flex:0 0 auto">' + _esc(m.statusText) + "</span>"
    + '<span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#bbb">'
    + _esc(m.account_id) + "：" + _esc(m.preview) + "</span>"
    + btns + "</div>";
}

// 失配持续时长 → 人话（秒/分/时）。
function formatDuration(secs) {
  const n = Math.max(0, Math.floor(Number(secs) || 0));
  if (n < 60) return n + " 秒";
  if (n < 3600) {
    const m = Math.floor(n / 60);
    const s = n % 60;
    return s ? m + " 分 " + s + " 秒" : m + " 分";
  }
  const h = Math.floor(n / 3600);
  const m = Math.floor((n % 3600) / 60);
  return m ? h + " 时 " + m + " 分" : h + " 时";
}

// 注入状态码 → 中文（与 inject-status.js::deriveInjectState 对齐）。
function injectStatusText(status) {
  switch (status) {
    case "ok": return "注入正常";
    case "mismatch_composer": return "选择器失配（输入框）";
    case "mismatch_bubble": return "选择器失配（消息）";
    case "unsupported": return "无注入档案";
    case "unknown": return "未上报";
    default: return String(status || "未知");
  }
}

// 单个账号 → 行展示模型。stale（数据陈旧）单列，避免与失配混淆。
function accountRowModel(a) {
  const acc = a || {};
  const status = acc.status || "unknown";
  const isMismatch = status === "mismatch_composer" || status === "mismatch_bubble";
  let cls = "ok";
  if (acc.stale) cls = "stale";
  else if (status === "unsupported") cls = "bad";
  else if (isMismatch) cls = "warn";
  else if (status !== "ok") cls = "wait";
  const ms = _num(acc, "mismatch_secs");
  return {
    cls,
    platform: String(acc.platform || ""),
    account_id: String(acc.account_id || ""),
    statusText: injectStatusText(status),
    stale: !!acc.stale,
    durationText: isMismatch && ms > 0 ? formatDuration(ms) : "",
  };
}

// 🔴 红点预警模型：持续失配（连续超阈值，非抖动）账号数 > 0 即亮。优先用 alerts.alerts 精确计数，
// 退回 injectHealthList.summary.persistent_mismatch。返回 {on,count,title}（title 同步到 🩺 图标 tooltip）。
// 人审 SLA 超时（纯函数）：held>0 且最久待审 ≥ 阈值秒 → 分级（warn/urgent）告警。
// 阈值优先取 payload 的 review_sla_sec/review_sla_urgent_sec（后端配置驱动），其次入参，再次默认。
function slaBreachModel(out, thresholdSec, urgentSec) {
  const o = out || {};
  const count = _num(o.summary, "held");
  const ageSec = _num(o, "review_oldest_age_sec");
  const warn = _num(o, "review_sla_sec") || thresholdSec || 300;
  let urgent = _num(o, "review_sla_urgent_sec") || urgentSec || warn * 3;
  if (urgent < warn) urgent = warn;
  let level = "none";
  if (count > 0) {
    if (ageSec >= urgent) level = "urgent";
    else if (ageSec >= warn) level = "warn";
  }
  return {
    breach: level !== "none", urgent: level === "urgent", level,
    ageSec, count, warnSec: warn, urgentSec: urgent,
  };
}

function alertDotModel(injectData, alertsData, sla) {
  let count = 0;
  if (alertsData && Array.isArray(alertsData.alerts)) {
    count = alertsData.alerts.length;
  } else if (injectData && injectData.summary) {
    count = _num(injectData.summary, "persistent_mismatch");
  }
  if (count > 0) {
    // 注入持续失配优先（更紧急：全自动发不出去）
    return {
      on: true,
      count,
      level: "mismatch",
      title: count + " 个账号注入持续失配（疑似官方网页改版，可热修 selector-profiles）",
    };
  }
  if (sla && sla.breach) {
    const urgent = sla.level === "urgent";
    return {
      on: true,
      count: sla.count,
      level: sla.level,
      title: (urgent ? "🔴 严重：" : "")
        + sla.count + " 条待审超时未处理（review_mode），客户可能"
        + (urgent ? "已流失——请立即放行/改写" : "久等——点开 🩺 尽快放行/改写"),
    };
  }
  return {
    on: false,
    count: 0,
    level: "none",
    title: "自动化健康：全账号注入命中 + 受控出站队列概览",
  };
}

// 覆写文件校验结果 → 展示模型（纯函数）。后端 validate 返回 {ok,exists,valid,profiles,dropped,error?}。
function formatValidateResult(r) {
  if (!r || !r.ok) return { cls: "bad", text: "校验失败：" + ((r && r.error) || "后端不可达") };
  if (!r.exists) return { cls: "wait", text: "无覆写文件（注入用内置档）" };
  if (!r.valid) return { cls: "bad", text: "JSON 无效：" + (r.error || "解析失败") };
  const dropped = Array.isArray(r.dropped) ? r.dropped : [];
  let text = "有效 ✓ " + (_num(r, "profiles")) + " 个平台覆写";
  if (dropped.length) {
    text += " · 已忽略 " + dropped.length + " 项（"
      + dropped.slice(0, 3).join("、") + (dropped.length > 3 ? "…" : "") + "）";
  }
  return { cls: dropped.length ? "warn" : "ok", text };
}

const _CLS_COLOR = {
  ok: "#2ecc71", warn: "#f1c40f", bad: "#e74c3c", wait: "#7f8c8d", stale: "#7f8c8d",
};

function _badgeHtml(b) {
  const color = _CLS_COLOR[b.cls] || "#7f8c8d";
  const hint = b.hint ? ' title="' + _esc(b.hint) + '"' : "";
  return '<span style="display:inline-block;padding:.1rem .5rem;border-radius:999px;'
    + 'font-size:.72rem;background:' + color + '22;color:' + color + ';border:1px solid ' + color + '55"'
    + hint + ">" + _esc(b.text) + "</span>";
}

function _esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// selector key → 人话标签（与后端 SELECTOR_KEYS 对齐）。
const SELECTOR_LABELS = {
  composer: "输入框", sendBtn: "发送按钮", bubble: "消息气泡", peerTitle: "对话标题",
};

// 逐选择器失配诊断（纯函数，P9）：优先用后端聚合 selector_diagnosis，缺失则从 alerts[].selectors
// 客户端兜底统计。让运营从「N 个账号失配」下钻到「哪个 selector key 抓空最多」，精准热修该键。
function selectorDiagnosisModel(alertsData) {
  const d = alertsData || {};
  let entries = [];
  if (Array.isArray(d.selector_diagnosis)) {
    entries = d.selector_diagnosis
      .map((e) => ({ key: String((e && e.key) || ""), missing: _num(e, "missing") }))
      .filter((e) => e.missing > 0);
  } else {
    const alerts = Array.isArray(d.alerts) ? d.alerts : [];
    const order = ["composer", "sendBtn", "bubble", "peerTitle"];
    const counts = {};
    for (const a of alerts) {
      const sel = (a && a.selectors) || {};
      for (const k of order) if (sel[k] === false) counts[k] = (counts[k] || 0) + 1;
    }
    entries = order.filter((k) => counts[k] > 0).map((k) => ({ key: k, missing: counts[k] }));
  }
  entries.sort((a, b) => b.missing - a.missing);
  entries.forEach((e) => { e.label = SELECTOR_LABELS[e.key] || e.key; });
  if (!entries.length) return { has: false, text: "", entries: [] };
  return {
    has: true, entries,
    text: "失配定位：" + entries.map((e) => e.label + " ✗" + e.missing).join(" · ")
      + " → 优先校准这些 selector key",
  };
}

// 「持续失配」告警块（纯函数）：仅当有 alerts 时渲染醒目红框，置于面板顶部引导 D1 热修；无则返回 ""。
function renderAlertsHtml(alertsData) {
  const alerts = alertsData && Array.isArray(alertsData.alerts) ? alertsData.alerts : [];
  if (!alerts.length) return "";
  let rows = "";
  for (const a of alerts.slice(0, 6)) {
    const m = accountRowModel(a);
    const dur = m.durationText ? " · 已持续 " + _esc(m.durationText) : "";
    rows += '<div style="display:flex;align-items:center;gap:.4rem;padding:.2rem 0;font-size:.74rem">'
      + '<span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'
      + _esc(m.platform) + " / " + _esc(m.account_id) + "</span>"
      + '<span style="color:#e74c3c;flex:0 0 auto">' + _esc(m.statusText) + dur + "</span>"
      + "</div>";
  }
  const diag = selectorDiagnosisModel(alertsData);
  const diagHtml = diag.has
    ? '<div style="font-size:.7rem;color:#e67e22;margin-top:.25rem;font-weight:600" '
      + 'title="跨失配账号统计各 selector key 抓空次数，定位官方改版到底改了哪个元素">'
      + _esc(diag.text) + "</div>"
    : "";
  return '<div style="background:#e74c3c1a;border:1px solid #e74c3c66;border-radius:8px;padding:.45rem .6rem;margin-bottom:.5rem">'
    + '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:.15rem">'
    + '<span style="font-size:.78rem;color:#e74c3c;font-weight:600">⚠ 注入持续失配（' + alerts.length + " 个账号）</span>"
    + '<button id="cp-health-fix" style="font-size:.7rem;padding:.12rem .55rem;border:1px solid #e74c3c88;border-radius:6px;background:#e74c3c22;color:#e74c3c;cursor:pointer" title="打开覆写文件，按平台填正确选择器即可热修（无需重发桌面包）">热修选择器</button>'
    + "</div>" + rows + diagHtml
    + '<div style="font-size:.68rem;color:#c0392b;margin-top:.2rem">疑似官方网页改版 → 点「热修选择器」打开 desktop_selector_profiles.json，保存后注入下次拉取即生效</div>'
    + "</div>";
}

// 近期拦截率读数（纯函数）：cancelled/(sent+failed+cancelled)。样本不足时不渲染颜色告警。
function interceptRateModel(out) {
  const o = out || {};
  const sample = _num(o, "intercept_sample");
  const rate = Number(o.intercept_rate);
  if (!sample || !isFinite(rate)) {
    return { pct: "—", text: "拦截率 —（样本不足）", cls: "ok" };
  }
  const pct = Math.round(rate * 100) + "%";
  const cls = rate >= 0.5 ? "bad" : rate >= 0.2 ? "warn" : "ok";
  return { pct, text: "近 7 日拦截率 " + pct + "（" + sample + " 条已审）", cls };
}

// 纠正样本读数（纯函数）：沉淀的「AI 失误/协同」数据量。无样本则不渲染。
function correctionsModel(out) {
  const c = (out && out.corrections) || {};
  const total = _num(c, "total");
  if (!total) return { has: false, text: "" };
  const edit = _num(c, "edit");
  const ai = _num(c, "ai_assisted");
  const aiPart = ai > 0 ? " · AI 协同 " + ai : "";
  return { has: true, text: "纠正样本 " + total + " 条（改写 " + edit + aiPart + "）" };
}

// 保存改写时的 action 载荷（纯函数）：据是否用过 AI 候选推断 source，凑黄金三元组。
function editSavePayload(id, text, aiSuggestion) {
  const t = String(text == null ? "" : text);
  const ai = String(aiSuggestion == null ? "" : aiSuggestion);
  let source = "human";
  if (ai) source = t.trim() === ai.trim() ? "ai_adopted" : "ai_edited";
  return { id, action: "edit", text: t, ai_suggestion: ai, source };
}

// 结构化拦截理由分类（P7）：code 稳定供聚类/ML，label 供展示。
const REASON_OPTIONS = [
  { code: "off_topic", label: "答非所问" },
  { code: "tone", label: "语气不当" },
  { code: "factual", label: "事实错误" },
  { code: "over_boundary", label: "越界违规" },
  { code: "redundant", label: "冗余" },
  { code: "other", label: "其他" },
];

function reasonLabel(code) {
  const f = REASON_OPTIONS.find((o) => o.code === code);
  return f ? f.label : String(code || "");
}

// 拦截理由 chips（纯函数）：点「拦截」展开，点某分类即带结构化 reason 拦截。
function renderInterceptChipsHtml(id) {
  let html = '<span style="font-size:.66rem;color:#e74c3c;flex:0 0 auto">拦截原因：</span>';
  for (const o of REASON_OPTIONS) {
    html += '<button data-cancel-reason="' + o.code + '" data-id="' + _esc(id) + '" '
      + 'style="font-size:.64rem;padding:.04rem .35rem;border:1px solid #e74c3c66;border-radius:5px;'
      + 'background:#e74c3c1a;color:#e74c3c;cursor:pointer;flex:0 0 auto">' + _esc(o.label) + "</button>";
  }
  html += '<button data-cancel-abort="1" style="font-size:.64rem;padding:.04rem .35rem;'
    + 'border:1px solid #ffffff33;border-radius:5px;background:transparent;color:inherit;cursor:pointer;flex:0 0 auto">✕</button>';
  return html;
}

// 失误聚类读数（纯函数）：reason_clusters → 按数量降序「答非所问 N · 事实错误 M」。
function reasonClusterModel(out) {
  const clusters = (out && out.reason_clusters) || {};
  const entries = Object.keys(clusters)
    .map((k) => ({ code: k, label: reasonLabel(k), count: _num(clusters, k) }))
    .filter((e) => e.count > 0)
    .sort((a, b) => b.count - a.count);
  if (!entries.length) return { has: false, text: "", entries: [] };
  return {
    has: true,
    entries,
    text: "失误聚类：" + entries.map((e) => e.label + " " + e.count).join(" · "),
  };
}

// 待审队列块（纯函数）：held 命令 FIFO + 批量「全部放行 / 全部拦截」。无待审则返回空串。
function renderReviewHtml(review) {
  const list = Array.isArray(review) ? review : [];
  if (!list.length) return "";
  let rows = "";
  for (const it of list.slice(0, 20)) {
    const m = Object.assign({}, it, { status: "held" });
    rows += renderOutboundRowHtml(m);
  }
  const ids = list.map((it) => it.id).filter((x) => x != null).join(",");
  return '<div style="border:1px solid #f39c1255;background:#f39c120f;border-radius:8px;'
    + 'padding:.4rem .5rem;margin-bottom:.45rem">'
    + '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:.15rem">'
    + '<strong style="font-size:.78rem;color:#f39c12">🔎 待审 ' + list.length + ' 条（先进先出）</strong>'
    + '<span style="display:flex;gap:.3rem">'
    + '<button data-bulk="release" data-ids="' + _esc(ids) + '" style="font-size:.66rem;'
    + 'padding:.05rem .4rem;border:1px solid #2ecc7188;border-radius:5px;background:#2ecc7122;'
    + 'color:#2ecc71;cursor:pointer">全部放行</button>'
    + '<button data-bulk="cancel" data-ids="' + _esc(ids) + '" style="font-size:.66rem;'
    + 'padding:.05rem .4rem;border:1px solid #e74c3c88;border-radius:5px;background:#e74c3c22;'
    + 'color:#e74c3c;cursor:pointer">全部拦截</button>'
    + "</span></div>" + rows + "</div>";
}

// 行内编辑器 HTML（纯函数）：输入框 + AI 重写 + 保存 + 取消。供 startInlineEdit 装配、可单测。
function renderInlineEditHtml(id) {
  return '<input class="cp-ob-edit" type="text" placeholder="输入改写后的内容…" '
    + 'style="flex:1;min-width:120px;font-size:.72rem;background:#0003;border:1px solid #ffffff33;'
    + 'border-radius:5px;color:inherit;padding:.15rem .35rem"/>'
    + '<button data-edit-airewrite="' + _esc(id) + '" title="按客户会话上下文生成更好的候选，可再编辑后保存" '
    + 'style="font-size:.66rem;padding:.05rem .4rem;border:1px solid #9b8cff88;border-radius:5px;'
    + 'background:#9b8cff22;color:#9b8cff;cursor:pointer;flex:0 0 auto">AI 重写</button>'
    + '<button data-edit-save="' + _esc(id) + '" style="font-size:.66rem;padding:.05rem .4rem;'
    + 'border:1px solid #2ecc7188;border-radius:5px;background:#2ecc7122;color:#2ecc71;cursor:pointer;flex:0 0 auto">保存</button>'
    + '<button data-edit-cancel="1" style="font-size:.66rem;padding:.05rem .4rem;'
    + 'border:1px solid #ffffff33;border-radius:5px;background:transparent;color:inherit;cursor:pointer;flex:0 0 auto">取消</button>';
}

// 完整面板 HTML（纯函数：便于单测，不碰 DOM）。alertsData 可选，给定时顶部渲染持续失配红框。
function renderPanelHtml(injectData, outboundData, alertsData) {
  const inj = injectData || {};
  const out = outboundData || {};
  const injB = injectBadge(inj.summary);
  const outB = outboundBadge(out.summary);
  const accounts = Array.isArray(inj.accounts) ? inj.accounts : [];
  const recent = Array.isArray(out.recent) ? out.recent : [];
  const review = Array.isArray(out.review) ? out.review : [];
  const rateM = interceptRateModel(out);
  const corrM = correctionsModel(out);
  const clusterM = reasonClusterModel(out);

  let rows = "";
  if (!accounts.length) {
    rows = '<div style="color:#7f8c8d;font-size:.74rem;padding:.3rem 0">暂无注入上报。</div>';
  } else {
    for (const a of accounts) {
      const m = accountRowModel(a);
      const color = _CLS_COLOR[m.cls] || "#7f8c8d";
      const dur = m.durationText ? ' · 已持续 ' + _esc(m.durationText) : "";
      const staleTag = m.stale ? ' · <span style="color:#7f8c8d">数据陈旧</span>' : "";
      rows += '<div style="display:flex;align-items:center;gap:.4rem;padding:.22rem 0;border-top:1px solid #ffffff14">'
        + '<span style="width:7px;height:7px;border-radius:50%;background:' + color + ';flex:0 0 auto"></span>'
        + '<span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.76rem">'
        + _esc(m.platform) + " / " + _esc(m.account_id) + "</span>"
        + '<span style="font-size:.72rem;color:' + color + '">' + _esc(m.statusText) + dur + staleTag + "</span>"
        + "</div>";
    }
  }

  let outRows = "";
  if (!recent.length) {
    outRows = '<div style="color:#7f8c8d;font-size:.74rem;padding:.3rem 0">暂无出站命令（未开 desktop_bridge / 无 desktop 账号全自动回复）。</div>';
  } else {
    for (const it of recent.slice(0, 8)) outRows += renderOutboundRowHtml(it);
  }

  return ''
    + '<div style="padding:.5rem .65rem">'
    + '  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:.35rem">'
    + '    <strong style="font-size:.82rem">🩺 自动化健康</strong>'
    + '    <button id="cp-health-refresh" style="font-size:.72rem;padding:.12rem .55rem;border:1px solid #ffffff33;border-radius:6px;background:transparent;color:inherit;cursor:pointer">刷新</button>'
    + "  </div>"
    + renderAlertsHtml(alertsData)
    + '  <div style="display:flex;flex-direction:column;gap:.5rem">'
    + '    <div>'
    + '      <div style="display:flex;align-items:center;gap:.4rem;margin-bottom:.2rem"><span style="font-size:.76rem;color:#bbb">注入命中</span>' + _badgeHtml(injB) + "</div>"
    + rows
    + '      <div style="display:flex;gap:.35rem;align-items:center;margin-top:.4rem;flex-wrap:wrap">'
    + '        <button id="cp-health-validate" style="font-size:.7rem;padding:.12rem .55rem;border:1px solid #ffffff33;border-radius:6px;background:transparent;color:inherit;cursor:pointer" title="校验 desktop_selector_profiles.json：JSON 是否合法、有无被忽略字段">校验覆写</button>'
    + '        <button id="cp-health-reload" style="font-size:.7rem;padding:.12rem .55rem;border:1px solid #ffffff33;border-radius:6px;background:transparent;color:inherit;cursor:pointer" title="重载内嵌官方页 → 注入重拉选择器（热修保存后点此即时生效，无需重启）">重载注入</button>'
    + '        <span id="cp-health-tools-msg" style="font-size:.7rem;color:#7f8c8d"></span>'
    + "      </div>"
    + "    </div>"
    + '    <div>'
    + '      <div style="display:flex;align-items:center;gap:.4rem;margin-bottom:.2rem;flex-wrap:wrap"><span style="font-size:.76rem;color:#bbb">受控出站</span>' + _badgeHtml(outB)
    + '<span style="font-size:.68rem;color:' + (_CLS_COLOR[rateM.cls] || "#7f8c8d") + '">' + _esc(rateM.text) + "</span>"
    + (corrM.has ? '<span style="font-size:.68rem;color:#9b8cff" title="人审改写/拦截沉淀的 AI 失误样本，供离线调优">' + _esc(corrM.text) + "</span>" : "")
    + (corrM.has ? '<button id="cp-corr-export" title="导出 JSONL 偏好对（rejected/chosen），喂 DPO/eval" style="font-size:.64rem;padding:.03rem .35rem;border:1px solid #9b8cff66;border-radius:5px;background:#9b8cff1a;color:#9b8cff;cursor:pointer">导出样本</button>' : "")
    + '<span id="cp-corr-export-msg" style="font-size:.66rem;color:#7f8c8d"></span>'
    + "</div>"
    + (clusterM.has ? '<div style="font-size:.66rem;color:#c39bd3;margin:.05rem 0 .1rem" title="人审拦截按原因聚类，看 AI 最常错在哪类">' + _esc(clusterM.text) + "</div>" : "")
    + renderReviewHtml(review)
    + outRows
    + "    </div>"
    + "  </div>"
    + '  <div style="margin-top:.45rem;font-size:.68rem;color:#7f8c8d">每 10 秒自动刷新 · 持续失配可改 config/desktop_selector_profiles.json 热修</div>'
    + "</div>";
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    injectBadge, outboundBadge, formatDuration, injectStatusText,
    accountRowModel, renderPanelHtml, alertDotModel, renderAlertsHtml,
    formatValidateResult, outboundStatusText, outboundActions,
    outboundRowModel, renderOutboundRowHtml,
    interceptRateModel, renderReviewHtml, correctionsModel,
    renderInlineEditHtml, editSavePayload, slaBreachModel,
    reasonLabel, renderInterceptChipsHtml, reasonClusterModel,
    selectorDiagnosisModel,
  };
}

// ── 浏览器 DOM 装配（Node 单测时跳过）────────────────────────────────────────
if (typeof document !== "undefined" && document.addEventListener) {
  document.addEventListener("DOMContentLoaded", () => {
    const toggle = document.getElementById("cp-health-toggle");
    const panel = document.getElementById("cp-health-panel");
    if (!toggle || !panel) return;
    const accountsPanel = document.getElementById("cp-accounts-panel");
    let timer = null;
    let bgTimer = null;

    // 🔴 在 🩺 图标右上角放一个红点（持续失配时亮）。复用同一 span，避免重复创建。
    function setAlertDot(model) {
      toggle.style.position = "relative";
      toggle.title = model.title;
      let dot = toggle.querySelector(".cp-health-dot");
      if (!model.on) {
        if (dot) dot.remove();
        return;
      }
      if (!dot) {
        dot = document.createElement("span");
        dot.className = "cp-health-dot";
        toggle.appendChild(dot);
      }
      // urgent → 深红 + 外发光高亮，与普通红点区分严重度
      const urgent = model.level === "urgent";
      dot.setAttribute(
        "style",
        "position:absolute;top:-2px;right:-2px;min-width:13px;height:13px;"
        + "padding:0 3px;border-radius:999px;color:#fff;"
        + "font-size:9px;line-height:13px;text-align:center;font-weight:700;"
        + (urgent
          ? "background:#c0392b;box-shadow:0 0 0 2px #c0392b66,0 0 6px #e74c3c;"
          : "background:#e74c3c;")
      );
      dot.textContent = model.count > 9 ? "9+" : String(model.count);
    }

    // 人审 SLA：阈值默认值（后端无配置时回落）；分级 warn/urgent 各只通知一次。
    const SLA_THRESHOLD_SEC = 300;
    let slaWarnNotified = false;
    let slaUrgentNotified = false;

    function _notify(title, body) {
      if (window.shell && window.shell.notify) {
        try { window.shell.notify({ title, body }); } catch (_) {}
      }
    }

    // 统一健康评估：注入失配 + 人审 SLA → 红点；SLA 超时分级去重通知。inj 后台轮询可为 null。
    function applyHealth(inj, al, out) {
      const sla = slaBreachModel(out, SLA_THRESHOLD_SEC);
      setAlertDot(alertDotModel(inj, al, sla));
      const mins = Math.round(sla.ageSec / 60);
      if (sla.level === "urgent") {
        slaWarnNotified = true;  // 已越过 warn 阶段
        if (!slaUrgentNotified) {
          slaUrgentNotified = true;
          _notify("🔴 待审严重超时",
            sla.count + " 条 AI 回复待审已超 " + mins + " 分钟，客户极可能流失，请立即处理");
        }
      } else if (sla.level === "warn") {
        if (!slaWarnNotified) {
          slaWarnNotified = true;
          _notify("待审超时",
            sla.count + " 条 AI 回复待人工审核已超 " + mins + " 分钟，请尽快放行/改写");
        }
      } else {
        slaWarnNotified = false;
        slaUrgentNotified = false;  // 超时解除 → 下次再超时可重新分级通知
      }
    }

    // 后台低频红点：取 alerts + 出站统计（含待审 SLA），面板未展开也能预警。
    async function refreshDot() {
      if (!window.shell || !window.shell.injectAlerts) return;
      try {
        const [al, out] = await Promise.all([
          window.shell.injectAlerts({}).catch(() => ({})),
          window.shell.outboundStats ? window.shell.outboundStats({}).catch(() => ({})) : Promise.resolve({}),
        ]);
        applyHealth(null, al, out);
        return al;
      } catch (e) {
        return null;
      }
    }

    async function refresh() {
      if (!window.shell || !window.shell.injectHealthList) return;
      try {
        const [inj, out, al] = await Promise.all([
          window.shell.injectHealthList({}).catch(() => ({})),
          window.shell.outboundStats({}).catch(() => ({})),
          window.shell.injectAlerts ? window.shell.injectAlerts({}).catch(() => ({})) : Promise.resolve({}),
        ]);
        panel.innerHTML = renderPanelHtml(inj, out, al);
        applyHealth(inj, al, out);
        const rb = document.getElementById("cp-health-refresh");
        if (rb) rb.addEventListener("click", () => { refresh().catch(() => {}); });
        const fx = document.getElementById("cp-health-fix");
        if (fx && window.shell && window.shell.openSelectors) {
          fx.addEventListener("click", async () => {
            fx.disabled = true;
            const prev = fx.textContent;
            fx.textContent = "打开中…";
            try {
              const r = await window.shell.openSelectors();
              fx.textContent = r && r.ok ? "已打开 ✓" : "打开失败";
            } catch (e) {
              fx.textContent = "打开失败";
            }
            setTimeout(() => { fx.textContent = prev; fx.disabled = false; }, 2500);
          });
        }
        const toolsMsg = document.getElementById("cp-health-tools-msg");
        const vb = document.getElementById("cp-health-validate");
        if (vb && window.shell && window.shell.validateSelectors) {
          vb.addEventListener("click", async () => {
            if (toolsMsg) toolsMsg.textContent = "校验中…";
            try {
              const r = await window.shell.validateSelectors();
              const m = formatValidateResult(r);
              if (toolsMsg) {
                toolsMsg.textContent = m.text;
                toolsMsg.style.color = _CLS_COLOR[m.cls] || "#7f8c8d";
              }
            } catch (e) {
              if (toolsMsg) toolsMsg.textContent = "校验失败";
            }
          });
        }
        const ex = document.getElementById("cp-corr-export");
        const exMsg = document.getElementById("cp-corr-export-msg");
        if (ex && window.shell && window.shell.exportCorrections) {
          ex.addEventListener("click", async () => {
            ex.disabled = true;
            if (exMsg) { exMsg.textContent = "导出中…"; exMsg.style.color = "#7f8c8d"; }
            try {
              const r = await window.shell.exportCorrections({});
              if (exMsg) {
                if (r && r.ok) {
                  exMsg.textContent = "已导出 " + r.count + " 条 ✓";
                  exMsg.style.color = "#2ecc71";
                } else if (r && r.canceled) {
                  exMsg.textContent = "";
                } else {
                  exMsg.textContent = (r && r.error) || "导出失败";
                  exMsg.style.color = "#e74c3c";
                }
              }
            } catch (e) {
              if (exMsg) { exMsg.textContent = "导出失败"; exMsg.style.color = "#e74c3c"; }
            }
            ex.disabled = false;
          });
        }
        const rl = document.getElementById("cp-health-reload");
        if (rl) {
          rl.addEventListener("click", () => {
            const wvs = document.querySelectorAll("#webviews webview");
            let n = 0;
            wvs.forEach((wv) => { try { wv.reload(); n++; } catch (e) { /* 忽略单个失败 */ } });
            if (toolsMsg) {
              toolsMsg.textContent = n ? "已重载 " + n + " 个内嵌页（注入将重拉选择器）"
                : "无内嵌官方页（当前为统一收件箱模式）";
              toolsMsg.style.color = "#7f8c8d";
            }
          });
        }
      } catch (e) {
        panel.innerHTML = '<div style="padding:.6rem;color:#e74c3c;font-size:.76rem">读取健康数据失败：'
          + _esc(String(e)) + "</div>";
      }
    }

    function show() {
      if (accountsPanel) accountsPanel.hidden = true;
      panel.hidden = false;
      refresh().catch(() => {});
      if (!timer) timer = setInterval(() => { refresh().catch(() => {}); }, 10000);
    }
    function hide() {
      panel.hidden = true;
      if (timer) { clearInterval(timer); timer = null; }
    }

    toggle.addEventListener("click", () => {
      if (panel.hidden) show(); else hide();
    });

    // 受控出站「人审介入」——委托点击（一次绑定；innerHTML 重渲不丢监听）。
    function startInlineEdit(btn, id) {
      const row = btn.closest("[data-oid]");
      if (!row) return;
      row.style.flexWrap = "wrap";
      row.innerHTML = renderInlineEditHtml(id);
      const inp = row.querySelector(".cp-ob-edit");
      if (inp) inp.focus();
    }

    // 拦截：展开结构化原因 chips（点分类即带 reason 拦截，留结构化负例样本）。
    function startInterceptReason(btn, id) {
      const row = btn.closest("[data-oid]");
      if (!row) return;
      row.style.flexWrap = "wrap";
      row.innerHTML = renderInterceptChipsHtml(id);
    }

    panel.addEventListener("click", async (e) => {
      const bulk = e.target.closest("[data-bulk]");
      if (bulk) {
        const action = bulk.getAttribute("data-bulk");
        const ids = (bulk.getAttribute("data-ids") || "").split(",").map(Number).filter(Boolean);
        if (ids.length && window.shell && window.shell.outboundAction) {
          if (action === "cancel" && typeof window.confirm === "function"
              && !window.confirm("确认拦截全部 " + ids.length + " 条待审命令？此操作不可撤销。")) {
            return;
          }
          bulk.disabled = true;
          try { await window.shell.outboundAction({ ids, action }); } catch (_) {}
        }
        refresh().catch(() => {});
        return;
      }
      const air = e.target.closest("[data-edit-airewrite]");
      if (air) {
        const id = Number(air.getAttribute("data-edit-airewrite"));
        const row = air.closest("[data-oid]");
        const inp = row && row.querySelector(".cp-ob-edit");
        if (!id || !window.shell || !window.shell.outboundRewrite) return;
        const prev = air.textContent;
        air.disabled = true;
        air.textContent = "生成中…";
        try {
          const r = await window.shell.outboundRewrite({ id });
          if (r && r.ok && r.reply) {
            if (inp) { inp.value = r.reply; inp.dataset.aiSuggestion = r.reply; inp.focus(); }
            air.textContent = "已填入 ✓";
          } else {
            air.textContent = r && r.detail ? "无上下文" : "失败";
          }
        } catch (_) {
          air.textContent = "失败";
        }
        setTimeout(() => { air.textContent = prev; air.disabled = false; }, 2500);
        return;
      }
      const save = e.target.closest("[data-edit-save]");
      if (save) {
        const id = Number(save.getAttribute("data-edit-save"));
        const row = save.closest("[data-oid]");
        const inp = row && row.querySelector(".cp-ob-edit");
        const text = inp ? inp.value : "";
        const aiSuggestion = inp && inp.dataset ? inp.dataset.aiSuggestion || "" : "";
        if (id && text.trim() && window.shell && window.shell.outboundAction) {
          try { await window.shell.outboundAction(editSavePayload(id, text, aiSuggestion)); } catch (_) {}
        }
        refresh().catch(() => {});
        return;
      }
      if (e.target.closest("[data-edit-cancel]")) { refresh().catch(() => {}); return; }
      const cr = e.target.closest("[data-cancel-reason]");
      if (cr) {
        const id = Number(cr.getAttribute("data-id"));
        const reason = cr.getAttribute("data-cancel-reason");
        if (id && window.shell && window.shell.outboundAction) {
          try { await window.shell.outboundAction({ id, action: "cancel", reason }); } catch (_) {}
        }
        refresh().catch(() => {});
        return;
      }
      if (e.target.closest("[data-cancel-abort]")) { refresh().catch(() => {}); return; }
      const btn = e.target.closest("[data-act]");
      if (!btn) return;
      const id = Number(btn.getAttribute("data-id"));
      const act = btn.getAttribute("data-act");
      if (!id) return;
      if (act === "edit") { startInlineEdit(btn, id); return; }
      if (act === "cancel") { startInterceptReason(btn, id); return; }
      if (!window.shell || !window.shell.outboundAction) return;
      btn.disabled = true;
      try { await window.shell.outboundAction({ id, action: act }); } catch (_) {}
      refresh().catch(() => {});
    });

    // 后台低频红点预警（面板未展开也提示）：首刷 + 每 60s。
    refreshDot().catch(() => {});
    bgTimer = setInterval(() => { if (panel.hidden) refreshDot().catch(() => {}); }, 60000);
    void bgTimer;
  });
}
