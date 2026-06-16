import { NextRequest, NextResponse } from "next/server";
import { setLeadStatus, refreshLeadCard, type LeadStatus } from "@/lib/lead-store";
import { requireAdmin } from "@/lib/admin-auth";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const authorized = requireAdmin;

const VALID: LeadStatus[] = ["new", "contacted", "won", "lost"];

export async function POST(req: NextRequest) {
  if (!process.env.TELEGRAM_SETUP_KEY) {
    return NextResponse.json({ ok: false, error: "not_configured" }, { status: 503 });
  }
  if (!authorized(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const body = await req.json().catch(() => null);
  const id = String(body?.id ?? "");
  const status = String(body?.status ?? "") as LeadStatus;
  if (!id || !VALID.includes(status)) {
    return NextResponse.json({ ok: false, error: "bad_request" }, { status: 400 });
  }
  const entry = await setLeadStatus(id, status);
  if (!entry) return NextResponse.json({ ok: false, error: "not_found" }, { status: 404 });
  await refreshLeadCard(entry);
  return NextResponse.json({ ok: true, status: entry.status });
}
