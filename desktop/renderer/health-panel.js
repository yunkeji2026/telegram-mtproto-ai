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

// 受控出站队列徽标：failed 优先告警，其次活动中（pending/claimed），否则空闲。
function outboundBadge(summary) {
  const s = summary || {};
  const failed = _num(s, "failed");
  const pending = _num(s, "pending");
  const claimed = _num(s, "claimed");
  if (failed > 0) {
    return { cls: "bad", text: failed + " 条发送失败", hint: "DOM 发送未成功（已记 failed，非误判已送达）" };
  }
  if (pending + claimed > 0) {
    return { cls: "warn", text: "活动中：待发 " + pending + " · 发送中 " + claimed, hint: "" };
  }
  return { cls: "ok", text: "队列空闲", hint: "" };
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
function alertDotModel(injectData, alertsData) {
  let count = 0;
  if (alertsData && Array.isArray(alertsData.alerts)) {
    count = alertsData.alerts.length;
  } else if (injectData && injectData.summary) {
    count = _num(injectData.summary, "persistent_mismatch");
  }
  const on = count > 0;
  return {
    on,
    count,
    title: on
      ? count + " 个账号注入持续失配（疑似官方网页改版，可热修 selector-profiles）"
      : "自动化健康：全账号注入命中 + 受控出站队列概览",
  };
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
  return '<div style="background:#e74c3c1a;border:1px solid #e74c3c66;border-radius:8px;padding:.45rem .6rem;margin-bottom:.5rem">'
    + '<div style="font-size:.78rem;color:#e74c3c;font-weight:600;margin-bottom:.15rem">⚠ 注入持续失配（'
    + alerts.length + " 个账号）</div>" + rows
    + '<div style="font-size:.68rem;color:#c0392b;margin-top:.2rem">疑似官方网页改版 → 改 config/desktop_selector_profiles.json 热修（无需重发桌面包）</div>'
    + "</div>";
}

// 完整面板 HTML（纯函数：便于单测，不碰 DOM）。alertsData 可选，给定时顶部渲染持续失配红框。
function renderPanelHtml(injectData, outboundData, alertsData) {
  const inj = injectData || {};
  const out = outboundData || {};
  const injB = injectBadge(inj.summary);
  const outB = outboundBadge(out.summary);
  const accounts = Array.isArray(inj.accounts) ? inj.accounts : [];
  const recent = Array.isArray(out.recent) ? out.recent : [];

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
    for (const it of recent.slice(0, 8)) {
      const st = String(it.status || "");
      const color = st === "failed" ? "#e74c3c" : st === "sent" ? "#2ecc71" : st === "claimed" ? "#f1c40f" : "#7f8c8d";
      outRows += '<div style="display:flex;gap:.4rem;padding:.2rem 0;border-top:1px solid #ffffff14;font-size:.73rem">'
        + '<span style="color:' + color + ';flex:0 0 auto">' + _esc(st) + "</span>"
        + '<span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#bbb">'
        + _esc(it.account_id || "") + "：" + _esc(it.preview || "") + "</span>"
        + "</div>";
    }
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
    + "    </div>"
    + '    <div>'
    + '      <div style="display:flex;align-items:center;gap:.4rem;margin-bottom:.2rem"><span style="font-size:.76rem;color:#bbb">受控出站</span>' + _badgeHtml(outB) + "</div>"
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
        dot.setAttribute(
          "style",
          "position:absolute;top:-2px;right:-2px;min-width:13px;height:13px;"
          + "padding:0 3px;border-radius:999px;background:#e74c3c;color:#fff;"
          + "font-size:9px;line-height:13px;text-align:center;font-weight:700;"
        );
        toggle.appendChild(dot);
      }
      dot.textContent = model.count > 9 ? "9+" : String(model.count);
    }

    // 仅取 alerts（轻）更新红点——后台低频与面板高频共用。
    async function refreshDot() {
      if (!window.shell || !window.shell.injectAlerts) return;
      try {
        const al = await window.shell.injectAlerts({}).catch(() => ({}));
        setAlertDot(alertDotModel(null, al));
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
        setAlertDot(alertDotModel(inj, al));
        const rb = document.getElementById("cp-health-refresh");
        if (rb) rb.addEventListener("click", () => { refresh().catch(() => {}); });
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

    // 后台低频红点预警（面板未展开也提示）：首刷 + 每 60s。
    refreshDot().catch(() => {});
    bgTimer = setInterval(() => { if (panel.hidden) refreshDot().catch(() => {}); }, 60000);
    void bgTimer;
  });
}
