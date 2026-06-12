import { readFile, writeFile, mkdir, rename } from "fs/promises";
import path from "path";
import { gatherHealth, type HealthResult } from "./health";
import { getAdminChats } from "./admin-store";
import { appendAlert } from "./alert-log";
import { SITE_URL } from "./site";
import { DATA_DIR } from "./data-dir";

const DIR = DATA_DIR;
const STATE = process.env.HEALTH_STATE || path.join(DIR, "health-state.json");
const RE_ALERT_MS = Number(process.env.HEALTH_REALERT_MS || 30 * 60 * 1000);

interface State {
  degraded: boolean;
  since?: number;
  lastAlertAt?: number;
  lastReasons?: string[];
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

/** Run a deep health check and alert admins on degraded/recovery transitions.
 *  `forceReasons` injects a synthetic degraded state for a safe end-to-end drill. */
export async function runHealthAlert(
  forceReasons?: string[]
): Promise<HealthResult & { alerted: boolean }> {
  const real = await gatherHealth(true);
  const h: HealthResult =
    forceReasons && forceReasons.length
      ? { ...real, healthy: false, status: "degraded", reasons: forceReasons }
      : real;
  const st = await readState();
  const now = Date.now();
  let alerted = false;

  if (!h.healthy) {
    const reasonsKey = h.reasons.join("|");
    const changed = !st.degraded || (st.lastReasons ?? []).join("|") !== reasonsKey;
    const stale = now - (st.lastAlertAt ?? 0) > RE_ALERT_MS;
    if (changed || stale) {
      const delivered = await notifyAdmins(
        `🚨 <b>服务降级告警</b>\n\n原因：${h.reasons.map((r) => `\n· ${r}`).join("")}\n\n时间：${h.time}\n详情：${SITE_URL}/api/health`
      );
      await appendAlert({ kind: "degrade", reasons: h.reasons, delivered: delivered > 0 });
      alerted = true;
    }
    await writeState({
      degraded: true,
      since: st.since ?? now,
      lastAlertAt: alerted ? now : st.lastAlertAt,
      lastReasons: h.reasons,
    });
  } else {
    if (st.degraded) {
      const downMs = st.since ? now - st.since : 0;
      const mins = Math.round(downMs / 60000);
      const delivered = await notifyAdmins(`✅ <b>服务已恢复正常</b>\n\n本次降级持续约 ${mins} 分钟。`);
      await appendAlert({ kind: "recover", reasons: [`down_${mins}min`], delivered: delivered > 0 });
      alerted = true;
    }
    await writeState({ degraded: false });
  }

  return { ...h, alerted };
}
