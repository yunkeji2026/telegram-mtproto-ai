"use strict";

// 注入诊断纯函数单测（无框架，node 直跑）：node test/inject-status.test.js
const assert = require("assert");
const { deriveInjectState } = require("../renderer/inject-status.js");

let pass = 0;
function check(name, got, expectCls) {
  assert.strictEqual(got.cls, expectCls, `${name}: 期望 cls=${expectCls}，实际 ${got.cls}`);
  assert.ok(got.text && got.detail, `${name}: text/detail 不应为空`);
  pass++;
}

// 无上报 → 等待
check("无上报", deriveInjectState(null), "wait");

// 平台无档案 → bad
check("无档案", deriveInjectState({ supported: false }), "bad");

// 已支持但未登录/未开会话（无输入框无会话）→ warn(未登录)
check("未登录", deriveInjectState({ supported: true, composer: false, bubbles: 0, chatOpen: false }), "warn");

// 会话开了但找不到输入框 → warn(输入框失配)
check("输入框失配", deriveInjectState({ supported: true, composer: false, bubbles: 5, chatOpen: true }), "warn");

// 输入框在但会话开着却抓不到消息 → warn(消息失配)
check("消息失配", deriveInjectState({ supported: true, composer: true, bubbles: 0, chatOpen: true }), "warn");

// 输入框 + 有消息 → ok
const ok = deriveInjectState({ supported: true, composer: true, bubbles: 8, chatOpen: true });
check("正常", ok, "ok");
assert.ok(ok.detail.indexOf("8") >= 0, "正常态 detail 应含消息数");

// 仅输入框在、无会话（刚登录未点会话）→ ok（composer 在即视为注入可用）
check("仅输入框", deriveInjectState({ supported: true, composer: true, bubbles: 0, chatOpen: false }), "ok");

console.log(`inject-status.test.js: ${pass} passed`);
