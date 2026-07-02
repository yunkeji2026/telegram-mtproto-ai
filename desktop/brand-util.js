"use strict";
/* 桌面壳品牌工具（纯函数，便于 node 直跑单测）。
   网络拉取 / electron nativeImage / setIcon 等副作用留在 main.js；
   这里只做：字段归一化、URL 拼接、默认 mark 判定、本地兜底选择。 */

const BRAND_FALLBACK = Object.freeze({
  product: "智聊",
  company: "无界科技",
  website: "https://usdt2026.cc",
  mark: null,
});

/** 把后端 /api/admin/branding 响应归一成统一形状；缺字段回落 fallback。 */
function normalizeLiveBrand(d, fallback = BRAND_FALLBACK) {
  if (!d || typeof d !== "object" || d.ok === false) return null;
  const s = (v) => String(v == null ? "" : v).trim();
  return {
    product: s(d.product_name) || fallback.product,
    company: s(d.company_name) || fallback.company,
    website: s(d.website_url) || fallback.website,
    mark: d.brand_mark_url || null,
  };
}

/** /static 相对路径 → 后端绝对 URL；已是 http(s) 原样返回；无 base 时原样返回。 */
function resolveBackendUrl(baseUrl, p) {
  const s = String(p || "").trim();
  if (!s) return "";
  if (/^https?:\/\//i.test(s)) return s;
  const base = String(baseUrl || "").replace(/\/+$/, "");
  return base ? `${base}${s.startsWith("/") ? "" : "/"}${s}` : s;
}

/** 是否内置无界默认 mark（默认已由本地图标覆盖，无需远程下载热替换）。 */
function isDefaultMark(mark) {
  return String(mark || "").endsWith("boundless-mark-256.png");
}

/** 本地兜底：config.brand → brand.json → 硬编码默认。 */
function pickBrandLocal(configBrand, brandJson, fallback = BRAND_FALLBACK) {
  const b = configBrand || {};
  if (b.website || b.product) return Object.assign({}, fallback, b);
  const j = brandJson || {};
  const p = j.product || {};
  const c = j.company || {};
  const links = j.links || {};
  const assets = j.assets || {};
  if (p.zh || c.zh || links.website || assets.mark) {
    return {
      product: p.zh || fallback.product,
      company: c.zh || fallback.company,
      website: links.website || fallback.website,
      mark: assets.mark || null,
    };
  }
  return Object.assign({}, fallback);
}

/** 依「当前生效 mark」与「上次已应用 mark」决策原生图标动作（纯函数，便于单测）。
 *  - custom  : 当前是白标自定义 logo 且与上次不同 → 需下载并热替换。
 *  - default : 当前回到默认/无 mark 但上次是自定义 → 需还原内置图标（白标改回默认）。
 *  - none    : 无变化 → 不动，避免无谓下载/闪烁。 */
function resolveIconAction(currentMark, lastAppliedMark) {
  const cur = currentMark || null;
  const last = lastAppliedMark || null;
  const curIsCustom = !!cur && !isDefaultMark(cur);
  const lastIsCustom = !!last && !isDefaultMark(last);
  if (curIsCustom) return { action: cur !== last ? "custom" : "none", mark: cur };
  if (lastIsCustom) return { action: "default", mark: null };
  return { action: "none", mark: null };
}

/** focus 可能高频触发；节流：距上次检查够久才允许再拉品牌。 */
function shouldCheckBrand(now, lastCheck, minIntervalMs = 3000) {
  return Number(now) - Number(lastCheck || 0) >= minIntervalMs;
}

module.exports = {
  BRAND_FALLBACK,
  normalizeLiveBrand,
  resolveBackendUrl,
  isDefaultMark,
  pickBrandLocal,
  resolveIconAction,
  shouldCheckBrand,
};
