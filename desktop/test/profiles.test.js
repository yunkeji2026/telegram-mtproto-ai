"use strict";

// 选择器档案纯函数单测（无框架/无 DOM，node 直跑）：node test/profiles.test.js
const assert = require("assert");
const {
  detectPlatform,
  makeGenericProfile,
  applySelectorOverlay,
  resolveProfile,
  BUILTIN_PROFILES,
  OVERLAYABLE_KEYS,
} = require("../inject/profiles.js");
const { needsChromeUa, urlNeedsChromeUa } = require("../webview-ua.js");

let pass = 0;
function ok(name, cond) {
  assert.ok(cond, name);
  pass++;
}

// ── detectPlatform：hostname → 平台 id ────────────────────────────────────────
ok("telegram", detectPlatform("web.telegram.org") === "telegram");
ok("whatsapp", detectPlatform("web.whatsapp.com") === "whatsapp");
ok("instagram", detectPlatform("www.instagram.com") === "instagram");
ok("messenger", detectPlatform("www.messenger.com") === "messenger");
ok("facebook→messenger", detectPlatform("www.facebook.com") === "messenger");
ok("x.com", detectPlatform("x.com") === "x");
ok("twitter→x", detectPlatform("twitter.com") === "x");
ok("zalo", detectPlatform("chat.zalo.me") === "zalo");
ok("line", detectPlatform("line.me") === "line");
ok("unknown", detectPlatform("example.com") === "unknown");

// ── BUILTIN_PROFILES：6 平台齐全且形态正确 ──────────────────────────────────
["telegram", "whatsapp", "instagram", "messenger", "x", "zalo"].forEach((p) => {
  ok(`builtin 有 ${p}`, !!BUILTIN_PROFILES[p]);
  ok(`${p} 有 bubble`, typeof BUILTIN_PROFILES[p].bubble === "string");
});
// 内置定制档默认可回流；通用工厂档默认关闭回流（宁缺毋错，待现场校准）
ok("telegram canIngest", BUILTIN_PROFILES.telegram.canIngest === true);
ok("whatsapp canIngest", BUILTIN_PROFILES.whatsapp.canIngest === true);
ok("instagram canIngest 默认关", BUILTIN_PROFILES.instagram.canIngest === false);
ok("x canIngest 默认关", BUILTIN_PROFILES.x.canIngest === false);
ok("通用档标记 generic", BUILTIN_PROFILES.zalo.generic === true);
ok("定制档非 generic", !BUILTIN_PROFILES.telegram.generic);

// ── makeGenericProfile：声明式 → 档案对象 ────────────────────────────────────
const g = makeGenericProfile({
  platform: "demo",
  bubble: ".b",
  bubbleText: ".t",
  composer: ".c",
  sendBtn: ".s",
  outFlag: "out",
});
ok("generic platform", g.platform === "demo");
ok("generic supported 默认真", g.supported === true);
ok("generic canIngest 默认假", g.canIngest === false);
ok("generic 有 text 函数", typeof g.text === "function");
ok("generic 有 isOut 函数", typeof g.isOut === "function");
// isOut 走 outFlag（classList 判定）
ok("isOut outFlag 命中", g.isOut({ classList: { contains: (x) => x === "out" } }) === true);
ok("isOut outFlag 未中", g.isOut({ classList: { contains: () => false } }) === false);
ok("isOut null 安全", g.isOut(null) === false);

// ── applySelectorOverlay：白名单覆盖 + 类型守卫 + 不改原对象 ──────────────────
const base = BUILTIN_PROFILES.instagram;
const patched = applySelectorOverlay(base, { bubble: ".new-bubble", canIngest: true });
ok("覆盖 bubble", patched.bubble === ".new-bubble");
ok("覆盖布尔 canIngest", patched.canIngest === true);
ok("原对象不变(bubble)", base.bubble !== ".new-bubble");
ok("原对象不变(canIngest)", base.canIngest === false);
ok("函数随原型保留", typeof patched.text === "function");

// 类型守卫：布尔字段拒收字符串；字符串字段拒收空串/非串；未知字段忽略
const guarded = applySelectorOverlay(base, {
  canIngest: "yes",        // 非布尔 → 忽略
  bubble: "",              // 空串 → 忽略
  composer: 123,           // 非串 → 忽略
  notAKey: "x",            // 非白名单 → 忽略
  sendBtn: ".ok-send",     // 合法 → 接受
});
ok("非布尔不覆盖 canIngest", guarded.canIngest === false);
ok("空串不覆盖 bubble", guarded.bubble === base.bubble);
ok("非串不覆盖 composer", guarded.composer === base.composer);
ok("未知键不进对象", guarded.notAKey === undefined);
ok("合法 sendBtn 覆盖", guarded.sendBtn === ".ok-send");

// null patch / null profile 安全
ok("null patch 返回浅拷贝", applySelectorOverlay(base, null).bubble === base.bubble);
ok("null profile 返回 null", applySelectorOverlay(null, {}) === null);

// ── resolveProfile：内置 → 覆写 → 兜底 ───────────────────────────────────────
ok("resolve 内置", resolveProfile("telegram", null).platform === "telegram");
ok(
  "resolve 覆写",
  resolveProfile("instagram", { instagram: { canIngest: true } }).canIngest === true
);
ok("resolve unsupported", resolveProfile("nope", null).supported === false);

// ── OVERLAYABLE_KEYS 与后端契约对齐（含关键字段）────────────────────────────
["bubble", "composer", "sendBtn", "canIngest"].forEach((k) =>
  ok(`overlayable 含 ${k}`, OVERLAYABLE_KEYS.indexOf(k) >= 0)
);

// ── webview-ua：多平台 Chrome UA 伪装判定 ────────────────────────────────────
ok("ua whatsapp", needsChromeUa("whatsapp") === true);
ok("ua instagram", needsChromeUa("instagram") === true);
ok("ua x", needsChromeUa("x") === true);
ok("ua telegram 不伪装", needsChromeUa("telegram") === false);
ok("ua url instagram", urlNeedsChromeUa("https://www.instagram.com/direct/inbox/") === true);
ok("ua url telegram 不伪装", urlNeedsChromeUa("https://web.telegram.org/k/") === false);

console.log(`profiles.test.js: ${pass} passed`);
