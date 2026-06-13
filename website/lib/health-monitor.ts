import { readFile, writeFile, mkdir, rename } from "fs/promises";
import path from "path";
import { gatherHealth, type HealthResult } from "./health";
import { getAdminChats } from "./admin-store";
import { appendAlert, listAlertsSince } from "./alert-log";
import { SITE_URL } from "./site";
import { DATA_DIR } from "./data-dir";

const DIR = DATA_DIR;
const STATE = process.env.HEALTH_STATE || path.join(DIR, "health-state.json");
const RE_ALERT_MS = Number(process.env.HEALTH_REALERT_MS || 30 * 60 * 1000);
// Only escalate to a strong push after this many consecutive degraded checks
// (filters transient single-cycle blips; cron runs ~每分钟 so default ≈3 分钟确认).
const ESCALATE_N = Math.max(1, Number(process.env.HEALTH_ESCALATE_N || 3));
// Daily digest is pushed once per local day at/after this hour (UTC+8).
const DIGEST_HOUR = Number(process.env.HEALTH_DIGEST_HOUR ?? 9);
const TZ_MS = 8 * 3600 * 1000;

interface State {
  degraded: boolean;
  since?: number;
  consec?: number; // consecutive degraded checks in the current episode
  escalated?: boolean; // whether a strong alert was already sent for this episode
  lastAlertAt?: number;
  lastReasons?: string[];
  lastDigestDay?: string; // local (UTC+8) day key already digested
}

async function readState(): Promise<State> {
  try {
    return JSON.parse(await readFile(STATE, "utf8"));
  } catch {
    return { degraded: false };
  }
}

async function writeState(s: State): Promise<void> {
  try {
    await mkdir(DIR, { recursive: true });
    const tmp = `${STATE}.${process.pid}.tmp`;
    await writeFile(tmp, JSON.stringify(s), "utf8");
    await rename(tmp, STATE);
  } catch {
    /* best-effort */
  }
}

// Returns the number of admin chats the alert was delivered to (0 = fallback to log only).
async function notifyAdmins(text: string): Promise<number> {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) return 0;
  const chats = await getAdminChats();
  if (chats.length === 0) return 0;
  const res = await Promise.allSettled(
    chats.map((chat) =>
      fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chat_id: chat, text, parse_mode: "HTML", disable_web_page_preview: true }),
      })
    )
  );
  return res.filter((r) => r.status === "fulfilled").length;
}

function localDayKey(ms: number): string {
  return new Date(ms + TZ_MS).toISOString().slice(0, 10);
}
function localHour(ms: number): number {
  return new Date(ms + TZ_MS).getUTCHours();
}

/** Send the once-per-day health digest if due; returns the day key when sent, else undefined.
 *  `force` (drill) sends regardless of schedule and does NOT consume the day slot. */
async function maybeDailyDigest(st: State, h: HealthResult, now: number, force = false): Promise<string | undefined> {
  const day = localDayKey(now);
  if (!force) {
    if (st.lastDigestDay === day) return undefined;
    if (localHour(now) < DIGEST_HOUR) return undefined;
  }

  const dayStartMs = Date.parse(day + "T00:00:00Z") - TZ_MS; // local midnight, in UTC ms
  const todays = await listAlertsSince(dayStartMs);
  const degradeN = todays.filter((a) => a.kind === "degrade").length;
  const recoverN = todays.filter((a) => a.kind === "recover").length;

  const usage = (h.checks.usage ?? {}) as { count?: number; cap?: number };
  const deepseek = (h.checks.deepseek ?? {}) as { circuit?: string };
  const tg = (h.checks.telegram ?? {}) as { ok?: boolean; bot?: string };

  const lines = [
    `📊 <b>每日健康日报</b> · ${day}`,
    ``,
    `状态：${h.healthy ? "✅ 正常运行" : "🚨 当前降级"}`,
    `今日告警：降级 ${degradeN} 次 · 恢复 ${recoverN} 次`,
    `DeepSeek：熔断 ${deepseek.circuit ?? "?"} · 今日用量 ${usage.count ?? 0}/${usage.cap ?? "?"}`,
    `Telegram Bot：${tg.ok ? `✅ @${tg.bot ?? "?"}` : "⚠️ 不可达"}`,
  ];
  if (!h.healthy) lines.push(``, `降级原因：${h.reasons.map((r) => `\n· ${r}`).join("")}`);
  lines.push(``, `详情：${SITE_URL}/api/health`);

  const delivered = await notifyAdmins(lines.join("\n"));
  await appendAlert({ kind: "digest", reasons: [`deg${degradeN}`, `rec${recoverN}`], delivered: delivered > 0 });
  return force ? undefined : day; // drills don't consume the day slot
}

/** Run a deep health check and alert admins on degraded/recovery transitions.
 *  Strong alerts only fire after ESCALATE_N consecutive degraded checks (anti-flap).
 *  Also pushes a once-per-day health digest.
 *  `forceReasons` injects a synthetic degraded state for a safe end-to-end drill. */
export async function runHealthAlert(
  forceReasons?: string[],
  opts?: { bypassThreshold?: boolean; forceDigest?: boolean }
): Promise<HealthResult & { alerted: boolean; consec: number; escalated: boolean }> {
  const real = await gatherHealth(true);
  const h: HealthResult =
    forceReasons && forceReasons.length
      ? { ...real, healthy: false, status: "degraded", reasons: forceReasons }
      : real;
  const st = await readState();
  const now = Date.now();
  let alerted = false;

  // Build the next state, then fold in the digest decision before a single write.
  const next: State = { ...st };

  if (!h.healthy) {
    const consec = (st.consec ?? 0) + 1;
    const reasonsKey = h.reasons.join("|");
    const reasonsChanged = (st.lastReasons ?? []).join("|") !== reasonsKey;
    const stale = now - (st.lastAlertAt ?? 0) > RE_ALERT_MS;
    // Page only after crossing the threshold; re-page if reasons changed or the alert went stale.
    const reached = opts?.bypassThreshold || consec >= ESCALATE_N;
    const shouldPage = reached && (!st.escalated || reasonsChanged || stale);

    if (shouldPage) {
      const downMins = Math.round((now - (st.since ?? now)) / 60000);
      const delivered = await notifyAdmins(
        `🚨 <b>服务降级告警</b>（连续 ${consec} 次确认）\n\n` +
          `原因：${h.reasons.map((r) => `\n· ${r}`).join("")}\n\n` +
          `已持续约 ${downMins} 分钟\n时间：${h.time}\n详情：${SITE_URL}/api/health`
      );
      await appendAlert({ kind: "degrade", reasons: h.reasons, delivered: delivered > 0, consec, escalated: true });
      alerted = true;
    }

    next.degraded = true;
    next.since = st.since ?? now;
    next.consec = consec;
    next.escalated = (st.escalated ?? false) || shouldPage;
    next.lastAlertAt = alerted ? now : st.lastAlertAt;
    next.lastReasons = h.reasons;
  } else {
    // Recovery is only worth paging if we actually escalated for this episode.
    if (st.degraded && st.escalated) {
      const mins = Math.round((st.since ? now - st.since : 0) / 60000);
      const delivered = await notifyAdmins(`✅ <b>服务已恢复正常</b>\n\n本次降级持续约 ${mins} 分钟。`);
      await appendAlert({ kind: "recover", reasons: [`down_${mins}min`], delivered: delivered > 0 });
      alerted = true;
    }
    next.degraded = false;
    next.since = undefined;
    next.consec = 0;
    next.escalated = false;
    next.lastReasons = [];
  }

  const digestDay = await maybeDailyDigest(st, h, now, opts?.forceDigest);
  if (digestDay) next.lastDigestDay = digestDay;

  await writeState(next);
  return { ...h, alerted, consec: next.consec ?? 0, escalated: next.escalated ?? false };
}
