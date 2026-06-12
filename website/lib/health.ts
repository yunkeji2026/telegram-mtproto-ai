import { mkdir, writeFile, unlink } from "fs/promises";
import path from "path";
import { breakerState } from "./circuit-breaker";
import { usageSnapshot } from "./chat-log";
import { DATA_DIR } from "./data-dir";

const DIR = DATA_DIR;

async function storageCheck(): Promise<{ ok: boolean; error?: string }> {
  try {
    await mkdir(DIR, { recursive: true });
    const probe = path.join(DIR, ".health-probe");
    await writeFile(probe, String(Date.now()), "utf8");
    await unlink(probe);
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String(e).slice(0, 120) };
  }
}

async function telegramCheck(): Promise<{ ok: boolean; bot?: string; error?: string }> {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) return { ok: false, error: "no_token" };
  try {
    const ac = new AbortController();
    const timer = setTimeout(() => ac.abort(), 5000);
    const res = await fetch(`https://api.telegram.org/bot${token}/getMe`, { signal: ac.signal });
    clearTimeout(timer);
    const data = await res.json();
    return data?.ok ? { ok: true, bot: data.result?.username } : { ok: false, error: data?.description };
  } catch (e) {
    return { ok: false, error: String(e).slice(0, 120) };
  }
}

export interface HealthResult {
  healthy: boolean;
  status: "healthy" | "degraded";
  time: string;
  reasons: string[];
  checks: Record<string, unknown>;
}

/** Single source of truth for health. `deep` performs live external calls (TG getMe). */
export async function gatherHealth(deep: boolean): Promise<HealthResult> {
  const env = {
    botToken: Boolean(process.env.TELEGRAM_BOT_TOKEN),
    deepseekKey: Boolean(process.env.DEEPSEEK_API_KEY),
    setupKey: Boolean(process.env.TELEGRAM_SETUP_KEY),
    adminKey: Boolean(process.env.ADMIN_KEY),
  };
  const storage = await storageCheck();
  const breaker = breakerState();
  const usage = usageSnapshot();

  const reasons: string[] = [];
  if (!env.botToken) reasons.push("no_bot_token");
  if (!storage.ok) reasons.push("storage_unwritable");
  if (breaker.state === "open") reasons.push(`deepseek_circuit_open(${breaker.lastError})`);
  if (usage.count >= usage.cap) reasons.push("daily_cap_reached");

  const checks: Record<string, unknown> = {
    env,
    storage,
    deepseek: {
      enabled: env.deepseekKey,
      circuit: breaker.state,
      consecutiveFailures: breaker.consecutiveFailures,
      openForMs: breaker.openForMs,
      totalTrips: breaker.totalTrips,
    },
    usage: { ...usage, remaining: Math.max(0, usage.cap - usage.count) },
  };

  if (deep) {
    const tg = await telegramCheck();
    checks.telegram = tg;
    if (!tg.ok) reasons.push(`telegram_unreachable(${tg.error ?? "?"})`);
  }

  const healthy = reasons.length === 0;
  return {
    healthy,
    status: healthy ? "healthy" : "degraded",
    time: new Date().toISOString(),
    reasons,
    checks,
  };
}
