// Build a multi-size favicon.ico (16/32/48) from the transparent brand mark.
// Next App Router serves app/favicon.ico at /favicon.ico for legacy browsers/bookmarks,
// alongside the modern app/icon.png. sharp can't write .ico, so we pack PNGs into ICO here.
// Run from website/:  node scripts/build-favicon.mjs

import { writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import sharp from "sharp";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const src = path.join(root, "public", "brand", "logos", "hualing-mark-256.png");
const out = path.join(root, "app", "favicon.ico");
const sizes = [16, 32, 48];

const pngs = await Promise.all(
  sizes.map((s) =>
    sharp(src).resize(s, s, { fit: "contain", background: { r: 0, g: 0, b: 0, alpha: 0 } }).png().toBuffer()
  )
);

// ICO: ICONDIR (6) + N * ICONDIRENTRY (16) + image data (PNG blobs)
const header = Buffer.alloc(6);
header.writeUInt16LE(0, 0); // reserved
header.writeUInt16LE(1, 2); // type = icon
header.writeUInt16LE(sizes.length, 4); // count

const entries = [];
let offset = 6 + 16 * sizes.length;
sizes.forEach((s, i) => {
  const e = Buffer.alloc(16);
  e.writeUInt8(s >= 256 ? 0 : s, 0); // width
  e.writeUInt8(s >= 256 ? 0 : s, 1); // height
  e.writeUInt8(0, 2); // palette
  e.writeUInt8(0, 3); // reserved
  e.writeUInt16LE(1, 4); // color planes
  e.writeUInt16LE(32, 6); // bits per pixel
  e.writeUInt32LE(pngs[i].length, 8); // size of image data
  e.writeUInt32LE(offset, 12); // offset
  offset += pngs[i].length;
  entries.push(e);
});

const ico = Buffer.concat([header, ...entries, ...pngs]);
await writeFile(out, ico);
console.log(`wrote app/favicon.ico (${sizes.join("/")}) — ${ico.length} bytes`);
