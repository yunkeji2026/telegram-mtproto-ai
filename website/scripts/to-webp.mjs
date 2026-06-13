// Generate .webp alongside every brand PNG (marks, lockups, avatars, icons) for
// lightweight external embedding (web / email / Telegram). The site itself uses
// next/image which already serves AVIF/WebP on demand — these files are for off-site use.
// Run from website/:  node scripts/to-webp.mjs

import { readdir, stat } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import sharp from "sharp";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const targets = [path.join(root, "public", "brand", "logos")];
const extraFiles = [path.join(root, "app", "icon.png"), path.join(root, "app", "apple-icon.png")];

async function listPngs(dir) {
  const out = [];
  for (const name of await readdir(dir)) {
    if (name.toLowerCase().endsWith(".png")) out.push(path.join(dir, name));
  }
  return out;
}

let pngTotal = 0;
let webpTotal = 0;

async function convert(file) {
  const out = file.replace(/\.png$/i, ".webp");
  // q90 lossy: gradients/3D marks compress beautifully with alpha preserved.
  await sharp(file).webp({ quality: 90, effort: 6, alphaQuality: 100 }).toFile(out);
  const a = (await stat(file)).size;
  const b = (await stat(out)).size;
  pngTotal += a;
  webpTotal += b;
  console.log(
    `  ${path.basename(out).padEnd(28)} ${(a / 1024).toFixed(0)}KB -> ${(b / 1024).toFixed(0)}KB`
  );
}

const files = [];
for (const d of targets) files.push(...(await listPngs(d)));
files.push(...extraFiles);

for (const f of files) await convert(f);

console.log(
  `done: ${files.length} files, ${(pngTotal / 1024).toFixed(0)}KB -> ${(webpTotal / 1024).toFixed(0)}KB ` +
    `(${(100 - (webpTotal / pngTotal) * 100).toFixed(0)}% smaller)`
);
