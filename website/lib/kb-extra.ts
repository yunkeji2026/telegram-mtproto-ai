// Operator-curated extra knowledge base.
// Lets admins answer "uncovered" questions from the dashboard; entries are
// injected into the LLM system prompt so the AI can answer them immediately.

import { appendFile, mkdir, readFile, writeFile } from "fs/promises";
import path from "path";
import { DATA_DIR } from "./data-dir";

const KB =
  process.env.KB_EXTRA_LOG ||
  path.join(DATA_DIR, "kb-extra.jsonl");

export interface KbEntry {
  id: string;
  q: string;
  a: string;
  lang?: string;
  t: string;
}

// short-lived cache so we don't hit disk on every chat request
let cache: { entries: KbEntry[]; at: number } | null = null;
const TTL = 10_000;

async function readAll(): Promise<KbEntry[]> {
  try {
    const raw = await readFile(KB, "utf-8");
    const out: KbEntry[] = [];
    for (const l of raw.split("\n")) {
      if (!l.trim()) continue;
      try {
        const o = JSON.parse(l);
        if (o && typeof o.q === "string" && typeof o.a === "string") out.push(o);
      } catch {
        /* skip bad line */
      }
    }
    return out;
  } catch {
    return [];
  }
}

export async function listKbEntries(): Promise<KbEntry[]> {
  return (await readAll()).slice().reverse();
}

export async function appendKbEntry(input: {
  q: string;
  a: string;
  lang?: string;
}): Promise<KbEntry> {
  const entry: KbEntry = {
    id: Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
    q: input.q.trim().slice(0, 300),
    a: input.a.trim().slice(0, 1200),
    lang: input.lang,
    t: new Date().toISOString(),
  };
  await mkdir(path.dirname(KB), { recursive: true });
  await appendFile(KB, JSON.stringify(entry) + "\n");
  cache = null;
  return entry;
}

export async function deleteKbEntry(id: string): Promise<boolean> {
  const all = await readAll();
  const next = all.filter((e) => e.id !== id);
  if (next.length === all.length) return false;
  await writeFile(KB, next.map((e) => JSON.stringify(e)).join("\n") + (next.length ? "\n" : ""));
  cache = null;
  return true;
}

/** Cached grounding block injected into the system prompt (empty string if none). */
export async function getKbExtraContext(): Promise<string> {
  const now = Date.now();
  if (!cache || now - cache.at > TTL) {
    cache = { entries: await readAll(), at: now };
  }
  const entries = cache.entries;
  if (entries.length === 0) return "";
  // most recent first, cap to keep the prompt lean
  const lines = entries
    .slice(-40)
    .reverse()
    .map((e) => `Q: ${e.q}\nA: ${e.a}`)
    .join("\n\n");
  return [
    "【运营补充问答 / Operator FAQ (authoritative, prefer these answers)】",
    lines,
  ].join("\n");
}
