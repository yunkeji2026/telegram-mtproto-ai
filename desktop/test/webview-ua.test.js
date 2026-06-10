"use strict";
const assert = require("assert");
const {
  chromeLikeUserAgent,
  isWhatsappPlatform,
  isWhatsappUrl,
  shouldSpoofWhatsappUa,
} = require("../webview-ua.js");

assert.ok(chromeLikeUserAgent("126.0.0.0").includes("Chrome/126.0.0.0"));
assert.ok(!chromeLikeUserAgent("126.0.0.0").includes("Electron"));
assert.strictEqual(isWhatsappPlatform("whatsapp"), true);
assert.strictEqual(isWhatsappPlatform("telegram"), false);
assert.strictEqual(isWhatsappUrl("https://web.whatsapp.com/"), true);
assert.strictEqual(isWhatsappUrl("https://web.telegram.org/"), false);
assert.strictEqual(shouldSpoofWhatsappUa("whatsapp", ""), true);
assert.strictEqual(shouldSpoofWhatsappUa("", "https://web.whatsapp.com/x"), true);
console.log("webview-ua.test.js: 7 passed");
