"use strict";

/* 把单一源 shared/inject/*.js 同步进扩展 vendor/（MV3 content_scripts 只能引用包内文件）。
 * 与 desktop/copy-shared.js 同理：唯一事实来源仍在 repo 根 shared/inject。
 * 用法：node extension/build.js（加载/打包扩展前先跑一次）。 */

const fs = require("fs");
const path = require("path");

const SRC = path.resolve(__dirname, "..", "shared", "inject");
const DST = path.join(__dirname, "vendor");
const FILES = ["profiles.js", "media-format.js", "core.js"];

try {
  if (!fs.existsSync(SRC)) {
    console.error(`[ext-build] 源不存在: ${SRC}`);
    process.exit(1);
  }
  fs.mkdirSync(DST, { recursive: true });
  for (const f of FILES) {
    fs.copyFileSync(path.join(SRC, f), path.join(DST, f));
  }
  console.log(`[ext-build] ok: ${SRC} → ${DST}（${FILES.join(", ")}）`);
} catch (e) {
  console.error(`[ext-build] 失败: ${e}`);
  process.exit(1);
}
