import { appendFile, mkdir, readFile } from "fs/promises";
import path from "path";
import { DATA_DIR } from "./data-dir";

const DIR = DATA_DIR;
const FILE = process.env.ALERT_LOG || path.join(DIR, "alerts.jsonl");

export interface AlertRec {
  t: string;
  kind: "degrade" | "recover";
  reasons: string[];
  delivered: boolean; // whether at least one admin chat received the push
}

export async function appendAlert(rec: Omit<AlertRec, "t">): Promise<void> {
  try {
    await mkdir(DIR, { recursive: true });
    await appendFile(FILE, JSON.stringify({ t: new Date().toISOString(), ...rec }) + "\n");
  } catch {
    /* best-effort */
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
