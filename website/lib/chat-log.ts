import { appendFile, mkdir, writeFile, rename } from "fs/promises";
import { readFileSync } from "fs";
import path from "path";
import { DATA_DIR } from "./data-dir";

const DIR = DATA_DIR;
const LOG = process.env.CHAT_LOG || path.join(DIR, "chats.jsonl");
const USAGE = process.env.CHAT_USAGE || path.join(DIR, "usage.json");

export async function logChat(rec: {
  q: string;
  a: string;
  lang: string;
  source: string;
  ip?: string;
}) {
  try {
    await mkdir(path.dirname(LOG), { recursive: true });
    await appendFile(
      LOG,
      JSON.stringify({
        t: new Date().toISOString(),
        q: rec.q.slice(0, 500),
        a: rec.a.slice(0, 1500),
        lang: rec.lang,
        source: rec.source,
        ip: (rec.ip ?? "").slice(0, 60),
      }) + "\n"
    );
  } catch {
    /* logging is best-effort */
  }
}

// ── global daily cost guard (persisted across restarts; resets each new day) ──
let day = "";
let count = 0;
let loaded = false;
let writeTimer: NodeJS.Timeout | null = null;

function loadUsage() {
  if (loaded) return;
  loaded = true;
  try {
    const raw = readFileSync(USAGE, "utf8");
    const p = JSON.parse(raw);
    if (p && typeof p.day === "string" && typeof p.count === "number") {
      day = p.day;
      count = p.count;
    }
  } catch {
    /* first run / no file */
  }
}

function persistUsage() {
  // debounce: at most one write per ~5s
  if (writeTimer) return;
  writeTimer = setTimeout(async () => {
    writeTimer = null;
    try {
      await mkdir(DIR, { recursive: true });
      const tmp = `${USAGE}.${process.pid}.tmp`;
      await writeFile(tmp, JSON.stringify({ day, count }), "utf8");
      await rename(tmp, USAGE);
    } catch {
      /* best-effort */
    }
  }, 5000);
  if (typeof writeTimer.unref === "function") writeTimer.unref();
}

export function dailyGuard(): { allowed: boolean; count: number; cap: number } {
  loadUsage();
  const today = new Date().toISOString().slice(0, 10);
  if (today !== day) {
    day = today;
    count = 0;
  }
  const cap = Number(process.env.CHAT_DAILY_CAP || 2000);
  if (count >= cap) return { allowed: false, count, cap };
  count += 1;
  persistUsage();
  return { allowed: true, count, cap };
}

/** Read-only snapshot for /api/health (does not increment). */
export function usageSnapshot(): { day: string; count: number; cap: number } {
  loadUsage();
  const today = new Date().toISOString().slice(0, 10);
  const cap = Number(process.env.CHAT_DAILY_CAP || 2000);
  return { day: today, count: today === day ? count : 0, cap };
}
