import { NextRequest, NextResponse } from "next/server";
import { readFile } from "fs/promises";
import path from "path";
import { clusterChats, type ChatRec } from "@/lib/chat-cluster";
import { listLeads } from "@/lib/lead-store";
import { unlockCounts } from "@/lib/unlock-store";
import { listPublishes } from "@/lib/publish-log";
import { requireAdmin } from "@/lib/admin-auth";
import { DATA_DIR, ANALYTICS_DIR } from "@/lib/data-dir";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const LEADS =
  process.env.LEADS_LOG || path.join(DATA_DIR, "leads.jsonl");
const EVENTS =
  process.env.ANALYTICS_LOG ||
  path.join(ANALYTICS_DIR, "events.jsonl");
const CHATS =
  process.env.CHAT_LOG || path.join(DATA_DIR, "chats.jsonl");

async function readJsonl(file: string, max = 5000): Promise<Record<string, unknown>[]> {
  try {
    const raw = await readFile(file, "utf-8");
    const lines = raw.split("\n").filter(Boolean).slice(-max);
    const out: Record<string, unknown>[] = [];
    for (const l of lines) {
      try {
        out.push(JSON.parse(l));
      } catch {
        /* skip bad line */
      }
    }
    return out;
  } catch {
    return [];
  }
}

// Bucket all day-level aggregates by a fixed offset (default UTC+8) so the
// operator's "today" matches local time rather than server UTC.
const TZ_OFFSET_H = Number(process.env.TZ_OFFSET ?? 8);
const TZ_MS = TZ_OFFSET_H * 3600 * 1000;

function dayKey(iso: unknown): string {
  const t = Date.parse(String(iso ?? ""));
  if (isNaN(t)) return "unknown";
  return new Date(t + TZ_MS).toISOString().slice(0, 10);
}

export async function GET(req: NextRequest) {
  if (!process.env.TELEGRAM_SETUP_KEY && !process.env.ADMIN_KEY) {
    return NextResponse.json({ ok: false, error: "not_configured" }, { status: 503 });
  }
  if (!requireAdmin(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }

  const [leads, events, chats] = await Promise.all([
    readJsonl(LEADS),
    readJsonl(EVENTS),
    readJsonl(CHATS),
  ]);

  const since = Date.now() - 7 * 24 * 3600 * 1000;
  const recent = (e: Record<string, unknown>) => {
    const t = Date.parse(String(e.t ?? e.ts ?? ""));
    return isNaN(t) ? true : t >= since;
  };

  // events breakdown
  const pageviews = events.filter((e) => e.event === "pageview");
  const ctaClicks = events.filter((e) => e.event === "cta_click");
  const leadSubmits = events.filter((e) => e.event === "lead_submit");

  // ── Mini App funnel (events: miniapp_*) ──
  const propStr = (e: Record<string, unknown>, k: string) => {
    const p = (e.props ?? {}) as Record<string, unknown>;
    const v = p[k];
    return v === undefined || v === null ? "" : String(v);
  };
  const miOpens = events.filter((e) => e.event === "miniapp_open");
  const miViews = events.filter((e) => e.event === "miniapp_view");
  const miCta = events.filter((e) => e.event === "miniapp_cta");
  const miLead = events.filter((e) => e.event === "miniapp_lead");
  const miUnlock = events.filter((e) => e.event === "miniapp_unlock");
  const miViewVisits: Record<string, number> = {};
  for (const e of miOpens) { const v = propStr(e, "view") || "home"; miViewVisits[v] = (miViewVisits[v] ?? 0) + 1; }
  for (const e of miViews) { const v = propStr(e, "view") || "?"; miViewVisits[v] = (miViewVisits[v] ?? 0) + 1; }
  const miCtaByView: Record<string, number> = {};
  for (const e of miCta) { const v = propStr(e, "view") || "?"; miCtaByView[v] = (miCtaByView[v] ?? 0) + 1; }
  const miOpenBySource: Record<string, number> = {};
  for (const e of miOpens) { const s = propStr(e, "source") || "direct"; miOpenBySource[s] = (miOpenBySource[s] ?? 0) + 1; }
  const miniapp = {
    opens: miOpens.length,
    cta: miCta.length,
    leads: miLead.length,
    unlocks: miUnlock.length,
    viewVisits: miViewVisits,
    ctaByView: miCtaByView,
    openBySource: miOpenBySource,
  };

  const ctaByWhere: Record<string, number> = {};
  for (const e of ctaClicks) {
    const props = (e.props ?? {}) as Record<string, unknown>;
    const w = String(props.where ?? "?");
    ctaByWhere[w] = (ctaByWhere[w] ?? 0) + 1;
  }

  // leads breakdown
  const leadsBySource: Record<string, number> = {};
  const leadsByInterest: Record<string, number> = {};
  const leadsByDay: Record<string, number> = {};
  for (const l of leads) {
    const src = String(l.source ?? "web");
    leadsBySource[src] = (leadsBySource[src] ?? 0) + 1;
    const it = String(l.interest ?? "-");
    leadsByInterest[it] = (leadsByInterest[it] ?? 0) + 1;
    leadsByDay[dayKey(l.t)] = (leadsByDay[dayKey(l.t)] ?? 0) + 1;
  }

  const leadEntries = await listLeads();
  const uniqueLeads = leadEntries.length;

  const pv = pageviews.length || events.filter((e) => e.event === "pageview").length;
  const convRate = pv > 0 ? (uniqueLeads / pv) * 100 : 0;

  const statusCounts: Record<string, number> = { new: 0, contacted: 0, won: 0, lost: 0 };
  for (const e of leadEntries) statusCounts[e.status] = (statusCounts[e.status] ?? 0) + 1;
  const recentLeads = leadEntries.slice(0, 40).map((l) => ({
    id: l.id,
    t: l.lastSeen,
    name: l.name,
    contact: l.contact,
    interest: l.interest,
    source: l.source ?? "web",
    lang: l.lang,
    status: l.status,
    count: l.count,
  }));

  const recentQuestions = chats
    .slice(-20)
    .reverse()
    .map((c) => ({ t: c.t, q: c.q, lang: c.lang, source: c.source }));

  const cluster = clusterChats(chats as ChatRec[]);
  const unlocks = await unlockCounts();

  // ---- 14-day daily series + week-over-week (real, derived from events/leads) ----
  const DAY = 24 * 3600 * 1000;
  const todayKey = new Date(Date.now() + TZ_MS).toISOString().slice(0, 10);
  const baseMs = Date.parse(todayKey + "T00:00:00Z");
  const days: string[] = [];
  const idx: Record<string, number> = {};
  for (let i = 13; i >= 0; i--) {
    const d = new Date(baseMs - i * DAY).toISOString().slice(0, 10);
    idx[d] = days.length;
    days.push(d);
  }
  const pvSeries = new Array(14).fill(0);
  const ctaSeries = new Array(14).fill(0);
  const leadSeries = new Array(14).fill(0);
  const bump = (arr: number[], iso: unknown) => {
    const d = dayKey(iso);
    if (d in idx) arr[idx[d]] += 1;
  };
  for (const e of pageviews) bump(pvSeries, e.t ?? e.ts);
  for (const e of ctaClicks) bump(ctaSeries, e.t ?? e.ts);
  for (const l of leads) bump(leadSeries, l.t);
  const sumRange = (arr: number[], a: number, b: number) =>
    arr.slice(a, b).reduce((x, y) => x + y, 0);
  const wow = {
    pageviews: { cur: sumRange(pvSeries, 7, 14), prev: sumRange(pvSeries, 0, 7) },
    ctaClicks: { cur: sumRange(ctaSeries, 7, 14), prev: sumRange(ctaSeries, 0, 7) },
    leads: { cur: sumRange(leadSeries, 7, 14), prev: sumRange(leadSeries, 0, 7) },
  };

  // ---- publish timeline (last 14 days) for impact attribution ----
  // We avoid claiming hard causal "uplift": instead we attach honest reference
  // figures (same-day PV/leads, next-day leads) and let the UI overlay markers
  // on the trend so the operator can judge correlation.
  const pubsRaw = await listPublishes(baseMs - 13 * DAY);
  const at = (arr: number[], i: number) => (i >= 0 && i < arr.length ? arr[i] : null);
  const publishes = pubsRaw.map((p) => {
    const d = dayKey(p.t);
    const i = d in idx ? idx[d] : -1;
    return {
      t: p.t,
      day: d,
      kind: p.kind,
      target: p.target,
      summary: p.summary,
      ref: {
        pvSame: at(pvSeries, i),
        leadSame: at(leadSeries, i),
        leadNext: at(leadSeries, i + 1),
      },
    };
  });
  // mark which day indices had at least one publish (for chart overlay)
  const publishDays = Array.from(new Set(publishes.map((p) => (p.day in idx ? idx[p.day] : -1)).filter((i) => i >= 0)));

  return NextResponse.json({
    ok: true,
    totals: {
      pageviews: pageviews.length,
      ctaClicks: ctaClicks.length,
      leadSubmitEvents: leadSubmits.length,
      leads: uniqueLeads,
      events: events.length,
      chats: chats.length,
      convRate: Number(convRate.toFixed(2)),
    },
    recentQuestions,
    last7d: {
      pageviews: pageviews.filter(recent).length,
      ctaClicks: ctaClicks.filter(recent).length,
      leads: leads.filter(recent).length,
    },
    ctaByWhere,
    leadsBySource,
    leadsByInterest,
    leadsByDay,
    recentLeads,
    statusCounts,
    unlocks,
    chatCluster: cluster,
    series: { days, pv: pvSeries, cta: ctaSeries, leads: leadSeries },
    wow,
    publishes,
    publishDays,
    miniapp,
  });
}
