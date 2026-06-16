"use strict";
/* prestart 钩子:把 repo 根的 shared/copilot 同步到 renderer/shared/copilot。
   桌面 CSP 'self' 需本地加载;单一事实来源仍在 repo 根 shared/copilot。 */
const fs = require("fs");
const path = require("path");

const SRC = path.resolve(__dirname, "..", "shared", "copilot");
const DST = path.join(__dirname, "renderer", "shared", "copilot");

function copyDir(src, dst) {
  fs.mkdirSync(dst, { recursive: true });
  for (const ent of fs.readdirSync(src, { withFileTypes: true })) {
    const s = path.join(src, ent.name);
    const d = path.join(dst, ent.name);
    if (ent.isDirectory()) copyDir(s, d);
    else fs.copyFileSync(s, d);
  }
}

try {
  if (!fs.existsSync(SRC)) {
    console.warn(`[copy-shared] 源不存在,跳过: ${SRC}`);
    process.exit(0);
  }
  copyDir(SRC, DST);
  console.log(`[copy-shared] ok: ${SRC} → ${DST}`);
} catch (e) {
  console.error(`[copy-shared] 失败: ${e}`);
  process.exit(0); // 不阻断启动
}
