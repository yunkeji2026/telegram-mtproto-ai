"use strict";

// 桌面壳「一号一指纹」注入（D3 防关联封号）。后端 /api/desktop/fingerprint 按 account_id 确定性派生，
// 主进程经 webPreferences.additionalArguments 把指纹随 preload 传入（race-free：在页面脚本读 navigator 前生效），
// 本模块在 preload 顶部 applyFingerprint(fp, window) 覆盖 navigator/Intl/WebGL/Canvas，使多号内嵌互不关联。
//
// 双模式：preload require（应用到真实 window）；Node 单测 require（纯函数 + 用 fake window 测 navigator/Intl）。

const FP_ARG_PREFIX = "--aitr-fp=";

// 把指纹编码进一个命令行参数（主进程侧用 → additionalArguments）。
function fingerprintArg(fp) {
  const json = JSON.stringify(fp || {});
  const b64 = typeof Buffer !== "undefined"
    ? Buffer.from(json, "utf8").toString("base64")
    : btoa(unescape(encodeURIComponent(json)));
  return FP_ARG_PREFIX + b64;
}

// 从 process.argv 解析出指纹（preload 侧）。无/损坏 → null。
function parseFpArg(argv) {
  const arr = argv || [];
  let hit = null;
  for (let i = 0; i < arr.length; i++) {
    if (typeof arr[i] === "string" && arr[i].indexOf(FP_ARG_PREFIX) === 0) { hit = arr[i]; break; }
  }
  if (!hit) return null;
  try {
    const b64 = hit.slice(FP_ARG_PREFIX.length);
    const json = typeof Buffer !== "undefined"
      ? Buffer.from(b64, "base64").toString("utf8")
      : decodeURIComponent(escape(atob(b64)));
    return JSON.parse(json);
  } catch (e) {
    return null;
  }
}

// 从 webview 分区名取 account_id（主进程 will-attach-webview 用）：persist:<id> → <id>。
function accountIdFromPartition(part) {
  const m = String(part || "").match(/^persist:(.+)$/);
  return m ? m[1] : "";
}

// djb2（与 profiles.js 同款）：Canvas 噪声用确定性扰动种子。
function _hash(s) {
  let h = 5381;
  const str = String(s || "");
  for (let i = 0; i < str.length; i++) h = ((h << 5) + h + str.charCodeAt(i)) | 0;
  return h >>> 0;
}

// 把指纹覆盖应用到 window。返回 {navigator, timezone, webgl, canvas} 各部分是否成功（便于自检/单测）。
function applyFingerprint(fp, win) {
  const w = win || (typeof window !== "undefined" ? window : null);
  const report = { navigator: false, timezone: false, webgl: false, canvas: false };
  if (!fp || !w) return report;

  function def(obj, prop, val) {
    try { Object.defineProperty(obj, prop, { get: function () { return val; }, configurable: true }); return true; }
    catch (e) { return false; }
  }

  // ① navigator：platform / languages / hardwareConcurrency / deviceMemory
  try {
    const nav = w.navigator;
    if (nav) {
      if (fp.platform) def(nav, "platform", fp.platform);
      if (Array.isArray(fp.languages) && fp.languages.length) {
        const langs = fp.languages.slice();
        def(nav, "languages", Object.freeze(langs));
        def(nav, "language", fp.language || langs[0]);
      }
      if (fp.hardware_concurrency) def(nav, "hardwareConcurrency", fp.hardware_concurrency);
      if (fp.device_memory) def(nav, "deviceMemory", fp.device_memory);
      report.navigator = true;
    }
  } catch (e) { /* navigator 覆盖失败：忽略 */ }

  // ② 时区：包装 Intl.DateTimeFormat().resolvedOptions().timeZone（指纹常查项）
  try {
    if (fp.timezone && w.Intl && w.Intl.DateTimeFormat) {
      const OrigDTF = w.Intl.DateTimeFormat;
      const wrap = function () {
        const inst = new OrigDTF(...arguments);
        const origResolved = inst.resolvedOptions.bind(inst);
        inst.resolvedOptions = function () {
          const o = origResolved();
          o.timeZone = fp.timezone;
          return o;
        };
        return inst;
      };
      wrap.prototype = OrigDTF.prototype;
      wrap.supportedLocalesOf = OrigDTF.supportedLocalesOf;
      w.Intl.DateTimeFormat = wrap;
      report.timezone = true;
    }
  } catch (e) { /* Intl 覆盖失败：忽略 */ }

  // ③ WebGL：UNMASKED_VENDOR/RENDERER（最常用于指纹的两个参数）
  try {
    const proto = w.WebGLRenderingContext && w.WebGLRenderingContext.prototype;
    if (proto && proto.getParameter && (fp.webgl_vendor || fp.webgl_renderer)) {
      const orig = proto.getParameter;
      proto.getParameter = function (p) {
        if (p === 37445 && fp.webgl_vendor) return fp.webgl_vendor;   // UNMASKED_VENDOR_WEBGL
        if (p === 37446 && fp.webgl_renderer) return fp.webgl_renderer; // UNMASKED_RENDERER_WEBGL
        return orig.call(this, p);
      };
      if (w.WebGL2RenderingContext && w.WebGL2RenderingContext.prototype &&
          w.WebGL2RenderingContext.prototype.getParameter) {
        const orig2 = w.WebGL2RenderingContext.prototype.getParameter;
        w.WebGL2RenderingContext.prototype.getParameter = function (p) {
          if (p === 37445 && fp.webgl_vendor) return fp.webgl_vendor;
          if (p === 37446 && fp.webgl_renderer) return fp.webgl_renderer;
          return orig2.call(this, p);
        };
      }
      report.webgl = true;
    }
  } catch (e) { /* WebGL 覆盖失败：忽略 */ }

  // ④ Canvas：toDataURL 时按种子施加 1px 级确定性微扰，使 hash 稳定且每号不同（不影响肉眼显示）
  try {
    const cproto = w.HTMLCanvasElement && w.HTMLCanvasElement.prototype;
    if (cproto && cproto.toDataURL && fp.canvas_noise_seed) {
      const seed = _hash(fp.canvas_noise_seed);
      const origToDataURL = cproto.toDataURL;
      cproto.toDataURL = function () {
        try {
          const ctx = this.getContext && this.getContext("2d");
          if (ctx && this.width > 0 && this.height > 0) {
            const x = seed % Math.max(1, this.width);
            const y = (seed >> 8) % Math.max(1, this.height);
            const img = ctx.getImageData(x, y, 1, 1);
            img.data[0] = (img.data[0] + (seed & 1)) & 255; // ±1 扰动
            ctx.putImageData(img, x, y);
          }
        } catch (e) { /* 跨域画布/无 2d：跳过扰动，仍走原 toDataURL */ }
        return origToDataURL.apply(this, arguments);
      };
      report.canvas = true;
    }
  } catch (e) { /* Canvas 覆盖失败：忽略 */ }

  return report;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    FP_ARG_PREFIX,
    fingerprintArg,
    parseFpArg,
    accountIdFromPartition,
    applyFingerprint,
  };
}
