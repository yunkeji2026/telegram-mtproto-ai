import { NextRequest, NextResponse } from "next/server";
import { runHealthAlert } from "@/lib/health-monitor";
import { gatherHealth } from "@/lib/health";
import { listAlerts } from "@/lib/alert-log";
import { requireAdmin } from "@/lib/admin-auth";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// Triggered by an external cron every minute; alerts admins on transitions.
// ?simulate=degrade forces one degraded cycle for a safe end-to-end alert drill.
export async function POST(req: NextRequest) {
  if (!requireAdmin(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const simulate = req.nextUrl.searchParams.get("simulate") === "degrade";
  const r = await runHealthAlert(simulate ? ["drill_test"] : undefined);
  return NextResponse.json({ ok: true, status: r.status, reasons: r.reasons, alerted: r.alerted });
}

// Dashboard snapshot: current health + recent alert history (no alerting side-effects).
export async function GET(req: NextRequest) {
  if (!requireAdmin(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const [health, alerts] = await Promise.all([gatherHealth(true), listAlerts(15)]);
  return NextResponse.json({ ok: true, health, alerts });
}
