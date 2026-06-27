"use strict";

// 指纹注入纯函数单测（无框架，node 直跑）：node test/fingerprint.test.js
const assert = require("assert");
const {
  FP_ARG_PREFIX,
  fingerprintArg,
  parseFpArg,
  accountIdFromPartition,
  applyFingerprint,
} = require("../inject/fingerprint.js");

let pass = 0;
function ok(name, cond) {
  assert.ok(cond, name);
  pass++;
}

const FP = {
  user_agent: "Mozilla/5.0 ... Chrome/124",
  platform: "Win32",
  os: "Windows",
  languages: ["zh-CN", "zh", "en"],
  language: "zh-CN",
  hardware_concurrency: 8,
  device_memory: 16,
  timezone: "Asia/Tokyo",
  webgl_vendor: "Google Inc. (Intel)",
  webgl_renderer: "ANGLE (Intel...)",
  canvas_noise_seed: "deadbeef",
};

// ── 编码/解码往返 ─────────────────────────────────────────────────────────────
const arg = fingerprintArg(FP);
ok("arg 带前缀", arg.indexOf(FP_ARG_PREFIX) === 0);
const parsed = parseFpArg(["--other", arg, "--x"]);
ok("解析往返一致", parsed && parsed.platform === "Win32" && parsed.timezone === "Asia/Tokyo");
ok("解析 languages", parsed.languages.join(",") === "zh-CN,zh,en");
ok("无参数 → null", parseFpArg(["--a", "--b"]) === null);
ok("损坏 base64 → null", parseFpArg([FP_ARG_PREFIX + "!!!notb64@@"]) === null);
ok("空 argv → null", parseFpArg([]) === null);

// ── 分区名 → account_id ───────────────────────────────────────────────────────
ok("persist 解析", accountIdFromPartition("persist:tg-desktop") === "tg-desktop");
ok("含冒号 id", accountIdFromPartition("persist:ig:99") === "ig:99");
ok("非 persist → 空", accountIdFromPartition("backend-workspace") === "");
ok("空 → 空", accountIdFromPartition("") === "");

// ── applyFingerprint：navigator + Intl（用 fake window）──────────────────────
function fakeWin() {
  const nav = {
    platform: "Linux x86_64",
    languages: ["en-US"],
    language: "en-US",
    hardwareConcurrency: 4,
    deviceMemory: 4,
  };
  // 让 defineProperty 能覆盖（默认对象属性可配置）
  const RealDTF = function (locale, opts) {
    return {
      resolvedOptions: function () { return { timeZone: "UTC", locale: locale || "en" }; },
    };
  };
  RealDTF.supportedLocalesOf = function () { return []; };
  return {
    navigator: nav,
    Intl: { DateTimeFormat: RealDTF },
  };
}

const w = fakeWin();
const rep = applyFingerprint(FP, w);
ok("navigator 报告成功", rep.navigator === true);
ok("timezone 报告成功", rep.timezone === true);
ok("platform 被覆盖", w.navigator.platform === "Win32");
ok("languages 被覆盖", w.navigator.languages.join(",") === "zh-CN,zh,en");
ok("language 被覆盖", w.navigator.language === "zh-CN");
ok("hardwareConcurrency 被覆盖", w.navigator.hardwareConcurrency === 8);
ok("deviceMemory 被覆盖", w.navigator.deviceMemory === 16);
// Intl 时区包装生效
const tz = new w.Intl.DateTimeFormat("en").resolvedOptions().timeZone;
ok("Intl 时区被覆盖", tz === "Asia/Tokyo");
// 无 WebGL/Canvas 类的环境 → 这两部分报告 false（不抛）
ok("无 WebGL 类 → false", rep.webgl === false);
ok("无 Canvas 类 → false", rep.canvas === false);

// ── 健壮性：空指纹 / 空 window 不抛 ──────────────────────────────────────────
ok("空 fp → 全 false", applyFingerprint(null, w).navigator === false);
ok("空 window → 全 false", applyFingerprint(FP, null).navigator === false);

console.log(`fingerprint.test.js: ${pass} passed`);
