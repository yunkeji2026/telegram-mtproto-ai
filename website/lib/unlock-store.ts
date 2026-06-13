import { mkdir, readFile, writeFile, rename } from "fs/promises";
import path from "path";
import { DATA_DIR } from "./data-dir";
import { BRAND } from "./brand";

const DIR = DATA_DIR;
const STORE = process.env.UNLOCK_STORE || path.join(DIR, "unlock-codes.json");

export interface UnlockRec {
  userId: number;
  code: string;
  contact?: string;
  name?: string;
  lang?: string;
  issuedAt: string;
  expiresAt?: string;
  redeemed: boolean;
  redeemedAt?: string;
}

export type RedeemResult =
  | { ok: true; rec: UnlockRec; alreadyRedeemed: boolean }
  | { ok: false; reason: "not_found" | "expired"; rec?: UnlockRec };

const TTL_DAYS = Number(process.env.UNLOCK_TTL_DAYS ?? 7);

/** A code is expired when it has an expiry in the past (legacy codes without expiry never expire). */
export function isExpired(rec: UnlockRec, now = Date.now()): boolean {
  if (!rec.expiresAt) return false;
  const t = Date.parse(rec.expiresAt);
  return !isNaN(t) && now > t;
}

interface UnlockDb {
  version: 1;
  byUser: Record<string, UnlockRec>;
}

let chain: Promise<unknown> = Promise.resolve();
function serialize<T>(fn: () => Promise<T>): Promise<T> {
  const next = chain.then(fn, fn);
  chain = next.catch(() => {});
  return next;
}

async function readDb(): Promise<UnlockDb> {
  try {
    const raw = await readFile(STORE, "utf-8");
    const parsed = JSON.parse(raw);
    if (parsed?.byUser) return parsed as UnlockDb;
  } catch {
    /* fresh */
  }
  return { version: 1, byUser: {} };
}

async function writeDb(db: UnlockDb) {
  await mkdir(DIR, { recursive: true });
  const tmp = STORE + ".tmp";
  await writeFile(tmp, JSON.stringify(db));
  await rename(tmp, STORE);
}

function genCode(userId: number): string {
  const base = Math.abs(userId).toString(36).toUpperCase().slice(-4);
  const rand = Math.random().toString(36).toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 3);
  return `${BRAND.discountPrefix}-${base}${rand}`;
}

/** Issue (or return existing) a one-time code for a verified, fully-joined user. */
export async function issueCode(
  userId: number,
  info?: { contact?: string; name?: string; lang?: string }
): Promise<UnlockRec> {
  return serialize(async () => {
    const db = await readDb();
    const key = String(userId);
    const existing = db.byUser[key];
    if (existing) {
      // enrich contact/name if newly available
      if (info?.contact && !existing.contact) existing.contact = info.contact;
      if (info?.name && !existing.name) existing.name = info.name;
      await writeDb(db);
      return existing;
    }
    const now = Date.now();
    const rec: UnlockRec = {
      userId,
      code: genCode(userId),
      contact: info?.contact,
      name: info?.name,
      lang: info?.lang,
      issuedAt: new Date(now).toISOString(),
      expiresAt: new Date(now + TTL_DAYS * 86400000).toISOString(),
      redeemed: false,
    };
    db.byUser[key] = rec;
    await writeDb(db);
    return rec;
  });
}

export async function redeemCode(code: string): Promise<RedeemResult> {
  const norm = code.trim().toUpperCase();
  return serialize(async () => {
    const db = await readDb();
    const rec = Object.values(db.byUser).find((r) => r.code === norm);
    if (!rec) return { ok: false, reason: "not_found" } as RedeemResult;
    // Already-redeemed codes can still be re-confirmed (idempotent); only block expiry on first use.
    if (!rec.redeemed && isExpired(rec)) {
      return { ok: false, reason: "expired", rec } as RedeemResult;
    }
    const alreadyRedeemed = rec.redeemed;
    if (!rec.redeemed) {
      rec.redeemed = true;
      rec.redeemedAt = new Date().toISOString();
      await writeDb(db);
    }
    return { ok: true, rec, alreadyRedeemed } as RedeemResult;
  });
}

export async function unlockCounts(): Promise<{
  issued: number;
  redeemed: number;
  pending: number;
  expired: number;
}> {
  const db = await readDb();
  const all = Object.values(db.byUser);
  const now = Date.now();
  const redeemed = all.filter((r) => r.redeemed).length;
  const expired = all.filter((r) => !r.redeemed && isExpired(r, now)).length;
  return {
    issued: all.length,
    redeemed,
    expired,
    pending: all.length - redeemed - expired,
  };
}

/** All issued codes, newest first. For the admin redemption dashboard. */
export async function listCodes(limit = 200): Promise<UnlockRec[]> {
  const db = await readDb();
  return Object.values(db.byUser)
    .sort((a, b) => (a.issuedAt < b.issuedAt ? 1 : -1))
    .slice(0, limit);
}

function clampDays(days: number): number {
  if (!Number.isFinite(days)) return 7;
  return Math.min(365, Math.max(1, Math.round(days)));
}

/**
 * Extend (or revive) a single unredeemed code's expiry by `days`.
 * Base is max(now, current expiry) so expired codes are revived and valid ones are pushed out.
 */
export async function extendCode(code: string, days: number): Promise<UnlockRec | null> {
  const norm = code.trim().toUpperCase();
  const d = clampDays(days);
  return serialize(async () => {
    const db = await readDb();
    const rec = Object.values(db.byUser).find((r) => r.code === norm);
    if (!rec || rec.redeemed) return null;
    const base = Math.max(Date.now(), rec.expiresAt ? Date.parse(rec.expiresAt) : Date.now());
    rec.expiresAt = new Date(base + d * 86400000).toISOString();
    await writeDb(db);
    return { ...rec };
  });
}

/** Extend every unredeemed code (pending + expired) by `days`. */
export async function extendUnredeemed(days: number): Promise<{ extended: number }> {
  const d = clampDays(days);
  return serialize(async () => {
    const db = await readDb();
    let extended = 0;
    const now = Date.now();
    for (const rec of Object.values(db.byUser)) {
      if (rec.redeemed) continue;
      const base = Math.max(now, rec.expiresAt ? Date.parse(rec.expiresAt) : now);
      rec.expiresAt = new Date(base + d * 86400000).toISOString();
      extended += 1;
    }
    if (extended) await writeDb(db);
    return { extended };
  });
}

/** Delete all expired, unredeemed codes (dead-code cleanup). */
export async function voidExpired(): Promise<{ removed: number }> {
  return serialize(async () => {
    const db = await readDb();
    const now = Date.now();
    let removed = 0;
    for (const [key, rec] of Object.entries(db.byUser)) {
      if (!rec.redeemed && isExpired(rec, now)) {
        delete db.byUser[key];
        removed += 1;
      }
    }
    if (removed) await writeDb(db);
    return { removed };
  });
}

/** Delete a single code by value. */
export async function deleteCode(code: string): Promise<boolean> {
  const norm = code.trim().toUpperCase();
  return serialize(async () => {
    const db = await readDb();
    const entry = Object.entries(db.byUser).find(([, r]) => r.code === norm);
    if (!entry) return false;
    delete db.byUser[entry[0]];
    await writeDb(db);
    return true;
  });
}

export interface RedemptionGroup {
  key: string;
  issued: number;
  redeemed: number;
  rate: number; // 0..1
}

/** Redemption-rate breakdown by language, plus overall. */
export async function redemptionStats(): Promise<{
  overall: RedemptionGroup;
  byLang: RedemptionGroup[];
}> {
  const db = await readDb();
  const all = Object.values(db.byUser);
  const groups = new Map<string, { issued: number; redeemed: number }>();
  let issued = 0;
  let redeemed = 0;
  for (const r of all) {
    issued += 1;
    if (r.redeemed) redeemed += 1;
    const lang = (r.lang || "未知").toLowerCase();
    const g = groups.get(lang) ?? { issued: 0, redeemed: 0 };
    g.issued += 1;
    if (r.redeemed) g.redeemed += 1;
    groups.set(lang, g);
  }
  const byLang: RedemptionGroup[] = [...groups.entries()]
    .map(([key, g]) => ({ key, issued: g.issued, redeemed: g.redeemed, rate: g.issued ? g.redeemed / g.issued : 0 }))
    .sort((a, b) => b.issued - a.issued);
  return {
    overall: { key: "all", issued, redeemed, rate: issued ? redeemed / issued : 0 },
    byLang,
  };
}
