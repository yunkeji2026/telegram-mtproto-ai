"use strict";

const DEFAULTS = {
  base_url: "http://127.0.0.1:18799",
  token: "admin",
  target_lang: "zh",
  auto_translate: false,
  sync_enabled: false,
  debug: false,
};

const TEXT_FIELDS = ["base_url", "token", "target_lang"];
const BOOL_FIELDS = ["auto_translate", "sync_enabled", "debug"];

function load() {
  chrome.storage.local.get(Object.keys(DEFAULTS), (got) => {
    const s = Object.assign({}, DEFAULTS, got || {});
    TEXT_FIELDS.forEach((k) => { document.getElementById(k).value = s[k] || ""; });
    BOOL_FIELDS.forEach((k) => { document.getElementById(k).checked = !!s[k]; });
  });
}

function save() {
  const out = {};
  TEXT_FIELDS.forEach((k) => { out[k] = (document.getElementById(k).value || "").trim() || DEFAULTS[k]; });
  BOOL_FIELDS.forEach((k) => { out[k] = !!document.getElementById(k).checked; });
  chrome.storage.local.set(out, () => {
    const el = document.getElementById("status");
    el.textContent = "已保存 ✓（刷新官方网页标签生效）";
    setTimeout(() => { el.textContent = ""; }, 2500);
  });
}

document.getElementById("save").addEventListener("click", save);

// 业务面板入口：把扩展与 Phase 1 的原生官网（统一收件箱 + copilot app.html）连起来。
document.getElementById("open-panel").addEventListener("click", () => {
  const base = (document.getElementById("base_url").value || DEFAULTS.base_url).trim().replace(/\/+$/, "");
  chrome.tabs.create({ url: base + "/workspace" });
});

load();
