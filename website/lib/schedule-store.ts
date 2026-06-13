import { mkdir, readFile, writeFile, rename } from "fs/promises";
import path from "path";
import { broadcastMessage, type BroadcastTarget } from "./tg-broadcast";
import { recordPublish } from "./publish-log";
import { DATA_DIR } from "./data-dir";

const DIR = DATA_DIR;
const STORE = process.env.SCHEDULE_STORE || path.join(DIR, "channel-posts.json");

export type PostStatus = "pending" | "sent" | "failed";

export interface ScheduledPost {
  id: string;
  text: string;
  target: BroadcastTarget;
  withButton: boolean;
  runAt: string; // ISO
  status: PostStatus;
  createdAt: string;
  sentAt?: string;
  error?: string;
}

export interface Template {
  id: string;
  name: string;
  text: string;
  target: BroadcastTarget;
  withButton: boolean;
}

export interface DraftPost {
  id: string;
  text: string;
  theme?: string;
  source: string;
  createdAt: string;
}

interface Db {
  version: 1;
  scheduled: ScheduledPost[];
  templates: Template[];
  drafts: DraftPost[];
}

let chain: Promise<unknown> = Promise.resolve();
function serialize<T>(fn: () => Promise<T>): Promise<T> {
  const next = chain.then(fn, fn);
  chain = next.catch(() => {});
  return next;
}

function rid() {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
}

async function readDb(): Promise<Db> {
  try {
    const raw = await readFile(STORE, "utf-8");
    const p = JSON.parse(raw);
    return { version: 1, scheduled: p.scheduled ?? [], templates: p.templates ?? [], drafts: p.drafts ?? [] };
  } catch {
    return { version: 1, scheduled: [], templates: [], drafts: [] };
  }
}

async function writeDb(db: Db) {
  await mkdir(DIR, { recursive: true });
  const tmp = STORE + ".tmp";
  await writeFile(tmp, JSON.stringify(db));
  await rename(tmp, STORE);
}

// ── scheduled posts ──
export async function listScheduled(): Promise<ScheduledPost[]> {
  const db = await readDb();
  return db.scheduled.slice().sort((a, b) => (a.runAt < b.runAt ? 1 : -1));
}

export async function addScheduled(input: {
  text: string;
  target: BroadcastTarget;
  withButton: boolean;
  runAt: string;
}): Promise<ScheduledPost> {
  return serialize(async () => {
    const db = await readDb();
    const post: ScheduledPost = {
      id: rid(),
      text: input.text.slice(0, 4000),
      target: input.target,
      withButton: input.withButton,
      runAt: input.runAt,
      status: "pending",
      createdAt: new Date().toISOString(),
    };
    db.scheduled.push(post);
    await writeDb(db);
    return post;
  });
}

export async function deleteScheduled(id: string): Promise<boolean> {
  return serialize(async () => {
    const db = await readDb();
    const n = db.scheduled.length;
    db.scheduled = db.scheduled.filter((p) => p.id !== id);
    if (db.scheduled.length === n) return false;
    await writeDb(db);
    return true;
  });
}

/** Reschedule a still-pending post to a new time (used by calendar drag-and-drop). */
export async function rescheduleScheduled(id: string, runAt: string): Promise<ScheduledPost | null> {
  return serialize(async () => {
    const db = await readDb();
    const p = db.scheduled.find((x) => x.id === id);
    if (!p) return null;
    if (p.status !== "pending") return null; // sent/failed posts are history; don't move
    p.runAt = runAt;
    await writeDb(db);
    return { ...p };
  });
}

// ── templates ──
export async function listTemplates(): Promise<Template[]> {
  return (await readDb()).templates;
}

export async function addTemplate(input: {
  name: string;
  text: string;
  target: BroadcastTarget;
  withButton: boolean;
}): Promise<Template> {
  return serialize(async () => {
    const db = await readDb();
    const tpl: Template = { id: rid(), name: input.name.slice(0, 60), text: input.text.slice(0, 4000), target: input.target, withButton: input.withButton };
    db.templates.push(tpl);
    await writeDb(db);
    return tpl;
  });
}

export async function deleteTemplate(id: string): Promise<boolean> {
  return serialize(async () => {
    const db = await readDb();
    const n = db.templates.length;
    db.templates = db.templates.filter((t) => t.id !== id);
    if (db.templates.length === n) return false;
    await writeDb(db);
    return true;
  });
}

// ── AI drafts (pending review) ──
export async function listDrafts(): Promise<DraftPost[]> {
  const db = await readDb();
  return db.drafts.slice().sort((a, b) => (a.createdAt < b.createdAt ? 1 : -1));
}

export async function addDraft(input: { text: string; theme?: string; source?: string }): Promise<DraftPost> {
  return serialize(async () => {
    const db = await readDb();
    const d: DraftPost = {
      id: rid(),
      text: input.text.slice(0, 4000),
      theme: input.theme,
      source: input.source ?? "ai",
      createdAt: new Date().toISOString(),
    };
    db.drafts.push(d);
    // keep last 30 drafts
    if (db.drafts.length > 30) db.drafts = db.drafts.slice(-30);
    await writeDb(db);
    return d;
  });
}

export async function deleteDraft(id: string): Promise<boolean> {
  return serialize(async () => {
    const db = await readDb();
    const n = db.drafts.length;
    db.drafts = db.drafts.filter((d) => d.id !== id);
    if (db.drafts.length === n) return false;
    await writeDb(db);
    return true;
  });
}

// ── runner: send all due pending posts (idempotent via status flip) ──
let running = false;

export async function runDuePosts(): Promise<{ ran: number }> {
  if (running) return { ran: 0 };
  running = true;
  try {
    const now = Date.now();
    // claim due posts atomically: flip them out of "pending" before sending
    const due = await serialize(async () => {
      const db = await readDb();
      const claim = db.scheduled.filter(
        (p) => p.status === "pending" && Date.parse(p.runAt) <= now
      );
      if (!claim.length) return [];
      for (const p of claim) p.status = "sent"; // optimistic claim; revert to failed on error
      await writeDb(db);
      return claim.map((p) => ({ ...p }));
    });

    let ran = 0;
    for (const post of due) {
      const res = await broadcastMessage({ text: post.text, target: post.target, withButton: post.withButton });
      ran += 1;
      await serialize(async () => {
        const db = await readDb();
        const p = db.scheduled.find((x) => x.id === post.id);
        if (!p) return;
        if (res.ok) {
          p.status = "sent";
          p.sentAt = new Date().toISOString();
          p.error = undefined;
          await recordPublish({ kind: "scheduled", target: post.target, summary: post.text });
        } else {
          p.status = "failed";
          p.error = res.results.map((r) => `${r.chat}:${r.error ?? "?"}`).join("; ").slice(0, 300);
        }
        await writeDb(db);
      });
    }
    return { ran };
  } finally {
    running = false;
  }
}
