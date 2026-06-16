"use strict";

// 内嵌账号工具纯函数：运行时新增/持久化「内嵌网页账号 Tab」用。
// 双模式：浏览器 <script> 加载成全局（renderer.js 用）；Node require 取 module.exports（单测）。

// 合并配置账号(base) 与本地持久化的运行时账号(saved)，按 id 去重 + 基本校验（须有 platform+url）。
// saved 一律打上 _auto:true，便于后续只持久化运行时新增的账号。
function mergeRuntimeAccounts(base, saved) {
  const out = (base || []).slice();
  const seen = new Set(out.map((a) => a && a.id).filter(Boolean));
  for (const a of saved || []) {
    if (!a || !a.id || seen.has(a.id)) continue;
    if (!a.platform || !a.url) continue;
    seen.add(a.id);
    out.push(Object.assign({}, a, { _auto: true }));
  }
  return out;
}

// 用平台模板(template=config.platforms[*]) + 入参组装一个运行时账号对象；缺 platform/url 返回 null。
function buildRuntimeAccount(opts) {
  opts = opts || {};
  const t = opts.template || {};
  const platform = opts.platform;
  const url = opts.url || t.url || "";
  if (!platform || !url || !opts.id) return null;
  return {
    id: opts.id,
    platform: platform,
    label: opts.label || t.name || platform,
    url: url,
    inject: opts.inject || t.inject || "",
    persona_id: opts.persona_id || t.persona_id || "",
    proxy: opts.proxy || "",
    _auto: true,
  };
}

// 从内存账号列表中挑出运行时新增账号(_auto)并裁剪成可持久化的最小字段，用于写 localStorage。
function serializeRuntimeAccounts(list) {
  return (list || [])
    .filter((a) => a && a._auto && a.id && a.platform && a.url)
    .map((a) => ({
      id: a.id,
      platform: a.platform,
      label: a.label || "",
      url: a.url,
      inject: a.inject || "",
      persona_id: a.persona_id || "",
      proxy: a.proxy || "",
      _auto: true,
    }));
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { mergeRuntimeAccounts, buildRuntimeAccount, serializeRuntimeAccounts };
}
