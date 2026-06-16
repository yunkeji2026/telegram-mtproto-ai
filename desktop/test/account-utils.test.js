"use strict";

// 内嵌账号工具纯函数单测（无框架，node 直跑）：node test/account-utils.test.js
const assert = require("assert");
const {
  mergeRuntimeAccounts,
  buildRuntimeAccount,
  serializeRuntimeAccounts,
} = require("../renderer/account-utils.js");

let pass = 0;
function ok(name, cond) {
  assert.ok(cond, name);
  pass++;
}

// ── mergeRuntimeAccounts ──────────────────────────────────────────────
const base = [{ id: "tg-desktop", platform: "telegram", url: "u" }];

// 正常合并，saved 打上 _auto
let m = mergeRuntimeAccounts(base, [{ id: "auto-1", platform: "telegram", url: "u2" }]);
ok("合并长度=2", m.length === 2);
ok("saved 标记 _auto", m[1]._auto === true);
ok("不污染 base", base.length === 1 && base[0]._auto === undefined);

// id 冲突去重（保留 base）
m = mergeRuntimeAccounts(base, [{ id: "tg-desktop", platform: "telegram", url: "x" }]);
ok("id 冲突去重", m.length === 1 && m[0].url === "u");

// 缺 platform/url 的 saved 被丢弃
m = mergeRuntimeAccounts(base, [{ id: "bad" }, { id: "no-url", platform: "telegram" }]);
ok("非法 saved 丢弃", m.length === 1);

// 空入参健壮
ok("空入参返回 []", mergeRuntimeAccounts(null, null).length === 0);

// ── buildRuntimeAccount ───────────────────────────────────────────────
const tpl = { name: "Telegram", url: "https://web.telegram.org/k/", inject: "tg-inject.js" };
const acc = buildRuntimeAccount({ id: "auto-x", platform: "telegram", template: tpl });
ok("用模板取 url", acc && acc.url === tpl.url);
ok("用模板取 inject", acc.inject === "tg-inject.js");
ok("缺 label 用模板名", acc.label === "Telegram");
ok("标记 _auto", acc._auto === true);
ok("缺 id 返回 null", buildRuntimeAccount({ platform: "telegram", template: tpl }) === null);
ok("缺模板且缺 url 返回 null", buildRuntimeAccount({ id: "z", platform: "telegram" }) === null);

// ── serializeRuntimeAccounts ──────────────────────────────────────────
const mixed = [
  { id: "tg-desktop", platform: "telegram", url: "u" }, // 非 _auto → 不持久化
  { id: "auto-1", platform: "telegram", url: "u2", label: "号2", _auto: true },
  { id: "bad", _auto: true }, // 非法 → 丢弃
];
const ser = serializeRuntimeAccounts(mixed);
ok("仅持久化 _auto 合法项", ser.length === 1 && ser[0].id === "auto-1");
ok("持久化字段完整", ser[0].url === "u2" && ser[0].label === "号2" && ser[0]._auto === true);

console.log(`account-utils.test.js: ${pass} passed`);
