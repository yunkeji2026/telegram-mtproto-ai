// One-off repo hygiene: downscale the 3 AI source marks to 1024px (longest side).
// They are GENERATOR INPUT only (not referenced at runtime — runtime uses hualing-mark-256.png),
// so 1024 is ample for every derived asset (largest consumer is the 512px avatar / ~400px lockup mark).
// Preserves alpha (these are already background-cut). Run from website/:
//   node scripts/slim-source-marks.mjs && node scripts/to-webp.mjs
import sharp from "sharp";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { stat, writeFile } from "node:fs/promises";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const logos = path.join(root, "public", "brand", "logos");
const marks = ["hualing-mark.png", "huaying-mark.png", "lingxi-mark.png"];
const TARGET = 1024;

let saved = 0;
for (const m of marks) {
  const p = path.join(logos, m);
  const before = (await stat(p)).size;
  const meta = await sharp(p).metadata();
  // Read fully into a buffer before writing back to the same path (avoids file-lock issues).
  const buf = await sharp(p)
    .resize(TARGET, TARGET, { fit: "inside", withoutEnlargement: true })
    .png({ compressionLevel: 9, effort: 10 })
    .toBuffer();
  await writeFile(p, buf);
  const after = (await stat(p)).size;
  saved += before - after;
  console.log(
    `${m}: ${meta.width}x${meta.height} ${(before / 1024).toFixed(0)}KB -> ${TARGET}px ${(after / 1024).toFixed(0)}KB`
  );
}
console.log(`total saved: ${(saved / 1024).toFixed(0)}KB`);
