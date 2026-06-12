import { appendFile, mkdir, readFile, writeFile, rename } from "fs/promises";
import path from "path";
import { getAdminChats } from "./admin-store";
import { DATA_DIR } from "./data-dir";

const DIR = DATA_DIR;
// append-only audit log (kept for raw history)
const LOG = process.env.LEADS_LOG || path.join(DIR, "leads.jsonl");
// keyed CRM store (deduped, with status)
const DB = process.env.LEADS_DB || path.join(DIR, "leads-db.json");

export type LeadStatus = "new" | "contacted" | "won" | "lost";

export interface LeadRecord {
  t: string;
  name: string;
  contact: string;
  interest: string;
  message: string;
  lang: string;
  source: string;
  verified?: string;
  tg_user_id?: string;
  path?: string;
  ref?: string;
  ua?: string;
  ip?: string;
}

export interface LeadEntry extends LeadRecord {
  id: string;
  status: LeadStatus;
  firstSeen: string;
  lastSeen: string;
  count: number;
  notifyChat?: number | string;
  notifyMsg?: number;
}

interface LeadDb {
  version: 1;
  leads: Record<string, LeadEntry>;
}

// ── single-process write serialization (avoids concurrent rewrite races) ──
let chain: Promise<unknown> = Promise.resolve();
function serialize<T>(fn: () => Promise<T>): Promise<T> {
  const next = chain.then(fn, fn);
  chain = next.catch(() => {});
  return next;
}

function dedupKey(rec: { tg_user_id?: string; contact: string }): string {
  if (rec.tg_user_id) return `tg:${rec.tg_user_id}`;
  return `c:${rec.contact.trim().toLowerCase().replace(/\s+/g, "")}`;
}

async function readDb(): Promise<LeadDb> {
  try {
    const raw = await readFile(DB, "utf-8");
    const parsed = JSON.parse(raw);
    if (parsed && parsed.leads) return parsed as LeadDb;
  } catch {
    /* fall through to migration */
  }
  return migrateFromJsonl();
}

/** One-time lazy migration: build the keyed store from the append-only log. */
async function migrateFromJsonl(): Promise<LeadDb> {
  const db: LeadDb = { version: 1, leads: {} };
  try {
    const raw = await readFile(LOG, "utf-8");
    for (const line of raw.split("\n")) {
      if (!line.trim()) continue;
      try {
        const r = JSON.parse(line) as LeadRecord;
        if (!r.contact) continue;
        upsertInto(db, r);
      } catch {
        /* skip bad line */
      }
    }
  } catch {
    /* no log yet */
  }
  return db;
}

function upsertInto(db: LeadDb, rec: LeadRecord): { entry: LeadEntry; isNew: boolean } {
  const key = dedupKey(rec);
  const existing = db.leads[key];
  if (existing) {
    existing.lastSeen = rec.t;
    existing.count += 1;
    // enrich with newer non-empty fields, keep status & firstSeen
    if (rec.name) existing.name = rec.name;
    if (rec.interest) existing.interest = rec.interest;
    if (rec.message) existing.message = rec.message;
    if (rec.lang) existing.lang = rec.lang;
    if (rec.source) existing.source = rec.source;
    if (rec.verified) existing.verified = rec.verified;
    if (rec.tg_user_id) existing.tg_user_id = rec.tg_user_id;
    if (rec.ip) existing.ip = rec.ip;
    return { entry: existing, isNew: false };
  }
  const entry: LeadEntry = {
    ...rec,
    id: key,
    status: "new",
    firstSeen: rec.t,
    lastSeen: rec.t,
    count: 1,
  };
  db.leads[key] = entry;
  return { entry, isNew: true };
}

async function writeDb(db: LeadDb) {
  await mkdir(DIR, { recursive: true });
  const tmp = DB + ".tmp";
  await writeFile(tmp, JSON.stringify(db));
  await rename(tmp, DB);
}

/** Append to the raw audit log (best-effort, keeps full history). */
export async function appendLead(rec: LeadRecord) {
  await mkdir(DIR, { recursive: true });
  await appendFile(LOG, JSON.stringify(rec) + "\n");
}

/** Upsert into the keyed CRM store with dedup; returns the merged entry. */
export async function upsertLead(rec: LeadRecord): Promise<{ entry: LeadEntry; isNew: boolean }> {
  return serialize(async () => {
    const db = await readDb();
    const res = upsertInto(db, rec);
    await writeDb(db);
    return res;
  });
}

export async function listLeads(): Promise<LeadEntry[]> {
  const db = await readDb();
  return Object.values(db.leads).sort((a, b) => (a.lastSeen < b.lastSeen ? 1 : -1));
}

export async function setLeadStatus(id: string, status: LeadStatus): Promise<LeadEntry | null> {
  return serialize(async () => {
    const db = await readDb();
    const e = db.leads[id];
    if (!e) return null;
    e.status = status;
    await writeDb(db);
    return e;
  });
}

export async function setLeadNotifyRef(id: string, chat: number | string, msg: number) {
  return serialize(async () => {
    const db = await readDb();
    const e = db.leads[id];
    if (!e) return;
    e.notifyChat = chat;
    e.notifyMsg = msg;
    await writeDb(db);
  });
}

const STATUS_LABEL: Record<LeadStatus, string> = {
  new: "🆕 新",
  contacted: "📞 已联系",
  won: "💰 已成交",
  lost: "🗑 已废弃",
};

function statusButtons(id: string): { inline_keyboard: { text: string; callback_data: string }[][] } {
  return {
    inline_keyboard: [
      [
        { text: "📞 已联系", callback_data: `lead:contacted:${id}` },
        { text: "💰 已成交", callback_data: `lead:won:${id}` },
        { text: "🗑 废弃", callback_data: `lead:lost:${id}` },
      ],
    ],
  };
}

function leadCard(entry: LeadEntry): string {
  return (
    `🆕 新留资 / New lead\n` +
    `👤 ${entry.name || "-"}\n` +
    `📞 ${entry.contact}\n` +
    `🎯 ${entry.interest || "-"}\n` +
    `📝 ${entry.message || "-"}\n` +
    `🌐 ${entry.lang || "-"} · ${entry.source || "web"}${entry.verified ? ` · ${entry.verified === "verified" ? "✅已验证" : "⚠️未验证"}` : ""}\n` +
    (entry.tg_user_id ? `🆔 TG: ${entry.tg_user_id}\n` : "") +
    (entry.count > 1 ? `🔁 第 ${entry.count} 次留资\n` : "") +
    `📍 ${entry.ip || "-"}\n` +
    `状态：${STATUS_LABEL[entry.status]}`
  );
}

export async function notifyAdminsOfLead(entry: LeadEntry) {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) return;
  const chats = await getAdminChats();
  if (!chats.length) return;
  const text = leadCard(entry);
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), 6000);
  try {
    const results = await Promise.allSettled(
      chats.map((chat) =>
        fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            chat_id: chat,
            text,
            disable_web_page_preview: true,
            reply_markup: statusButtons(entry.id),
          }),
        }).then((r) => r.json())
      )
    );
    // remember the first successful notification so callbacks can edit it
    for (const r of results) {
      if (r.status === "fulfilled" && r.value?.ok) {
        const m = r.value.result;
        await setLeadNotifyRef(entry.id, m.chat.id, m.message_id);
        break;
      }
    }
  } catch {
    /* best-effort */
  } finally {
    clearTimeout(timer);
  }
}

/** Re-render a lead notification card after a status change (best-effort). */
export async function refreshLeadCard(entry: LeadEntry) {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token || !entry.notifyChat || !entry.notifyMsg) return;
  const showButtons = entry.status === "new" || entry.status === "contacted";
  try {
    await fetch(`https://api.telegram.org/bot${token}/editMessageText`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: entry.notifyChat,
        message_id: entry.notifyMsg,
        text: leadCard(entry),
        disable_web_page_preview: true,
        reply_markup: showButtons ? statusButtons(entry.id) : { inline_keyboard: [] },
      }),
    });
  } catch {
    /* best-effort */
  }
}

export { STATUS_LABEL };
