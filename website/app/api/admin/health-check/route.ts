import { NextRequest, NextResponse } from "next/server";
import { runHealthAlert } from "@/lib/health-monitor";
import { gatherHealth } from "@/lib/health";
import { listAlerts } from "@/lib/alert-log";
import { requireAdmin } from "@/lib/admin-auth";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// Triggered by an external cron every minute; alerts admins on transitions.
// ?simulate=degrade forces one degraded cycle (bypasses the N-consecutive threshold) for a drill.
// ?digest=1 forces an immediate daily-digest push (does not consume the day slot).
export async function POST(req: NextRequest) {
  if (!requireAdmin(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const simulate = req.nextUrl.searchParams.get("simulate") === "degrade";
  const digest = req.nextUrl.searchParams.get("digest") === "1";
  const r = await runHealthAlert(simulate ? ["drill_test"] : undefined, {
    bypassThreshold: simulate,
    forceDigest: digest,
  });
  return NextResponse.json({
    ok: true,
    status: r.status,
    reasons: r.reasons,
    alerted: r.alerted,
    consec: r.consec,
    escalated: r.escalated,
  });
}

// Dashboard snapshot: current health + recent alert history (no alerting side-effects).
export async function GET(req: NextRequest) {
  if (!requireAdmin(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const [health, alerts] = await Promise.all([gatherHealth(true), listAlerts(15)]);
  return NextResponse.json({ ok: true, health, alerts });
}
