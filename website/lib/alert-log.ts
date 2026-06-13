import { appendFile, mkdir, readFile } from "fs/promises";
import path from "path";
import { DATA_DIR } from "./data-dir";

const DIR = DATA_DIR;
const FILE = process.env.ALERT_LOG || path.join(DIR, "alerts.jsonl");

export interface AlertRec {
  t: string;
  kind: "degrade" | "recover" | "digest";
  reasons: string[];
  delivered: boolean; // whether at least one admin chat received the push
  consec?: number; // consecutive degraded checks at the time of a degrade alert
  escalated?: boolean; // whether this degrade crossed the strong-alert threshold
}

export async function appendAlert(rec: Omit<AlertRec, "t">): Promise<void> {
  try {
    await mkdir(DIR, { recursive: true });
    await appendFile(FILE, JSON.stringify({ t: new Date().toISOString(), ...rec }) + "\n");
  } catch {
    /* best-effort */
  }
}

/** Alerts since a wall-clock timestamp (ms). Used by the daily digest. */
export async function listAlertsSince(sinceMs: number): Promise<AlertRec[]> {
  try {
    const raw = await readFile(FILE, "utf8");
    const out: AlertRec[] = [];
    for (const line of raw.split("\n").filter(Boolean)) {
      try {
        const rec = JSON.parse(line) as AlertRec;
        if (Date.parse(rec.t) >= sinceMs) out.push(rec);
      } catch {
        /* skip */
      }
    }
    return out;
  } catch {
    return [];
  }
}

export async function listAlerts(limit = 20): Promise<AlertRec[]> {
  try {
    const raw = await readFile(FILE, "utf8");
    const lines = raw.split("\n").filter(Boolean);
    const out: AlertRec[] = [];
    for (const line of lines.slice(-limit)) {
      try {
        out.push(JSON.parse(line));
      } catch {
        /* skip */
      }
    }
    return out.reverse();
  } catch {
    return [];
  }
}
