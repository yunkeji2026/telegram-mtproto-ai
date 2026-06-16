import { mkdir, readFile, appendFile } from "fs/promises";
import path from "path";
import { DATA_DIR } from "./data-dir";

// Append-only log of every content publish (broadcast / catalog / daily / scheduled).
// Used by the admin dashboard to overlay publish markers on trend charts and to
// surface a "publish timeline + same-day/next-day reference metrics" report.
// We deliberately avoid claiming hard causal "uplift" numbers — multiple posts in
// one day plus organic noise make that misleading. Markers + reference figures let
// the operator judge correlation honestly.

const DIR = DATA_DIR;
const LOG = process.env.PUBLISH_LOG || path.join(DIR, "publishes.jsonl");

export type PublishKind = "broadcast" | "catalog" | "daily" | "scheduled";

export interface PublishRec {
  t: string; // ISO timestamp
  kind: PublishKind;
  target: string; // channel | group | both | ...
  summary: string;
}

export async function recordPublish(rec: {
  kind: PublishKind;
  target: string;
  summary?: string;
}): Promise<void> {
  try {
    await mkdir(DIR, { recursive: true });
    const line =
      JSON.stringify({
        t: new Date().toISOString(),
        kind: rec.kind,
        target: rec.target,
        summary: (rec.summary ?? "").replace(/\s+/g, " ").trim().slice(0, 120),
      }) + "\n";
    await appendFile(LOG, line, "utf-8");
  } catch {
    // non-fatal: attribution logging must never break a publish flow
  }
}

export async function listPublishes(sinceMs?: number, max = 300): Promise<PublishRec[]> {
  try {
    const raw = await readFile(LOG, "utf-8");
    const out: PublishRec[] = [];
    const lines = raw.split("\n").filter(Boolean).slice(-max);
    for (const l of lines) {
      try {
        const r = JSON.parse(l) as PublishRec;
        if (!r?.t) continue;
        if (sinceMs) {
          const t = Date.parse(r.t);
          if (!isNaN(t) && t < sinceMs) continue;
        }
        out.push(r);
      } catch {
        // skip malformed line
      }
    }
    return out.reverse(); // newest first
  } catch {
    return [];
  }
}
