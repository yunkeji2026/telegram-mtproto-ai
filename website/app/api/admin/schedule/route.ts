import { NextRequest, NextResponse } from "next/server";
import { addScheduled, deleteScheduled, listScheduled, rescheduleScheduled } from "@/lib/schedule-store";
import type { BroadcastTarget } from "@/lib/tg-broadcast";
import { requireAdmin } from "@/lib/admin-auth";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const authorized = requireAdmin;

export async function GET(req: NextRequest) {
  if (!process.env.TELEGRAM_SETUP_KEY) {
    return NextResponse.json({ ok: false, error: "not_configured" }, { status: 503 });
  }
  if (!authorized(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  return NextResponse.json({ ok: true, scheduled: await listScheduled() });
}

export async function POST(req: NextRequest) {
  if (!authorized(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const body = await req.json().catch(() => null);
  const text = String(body?.text ?? "").trim();
  const target = String(body?.target ?? "channel") as BroadcastTarget;
  const withButton = body?.withButton !== false;
  const runAt = String(body?.runAt ?? "");
  const ts = Date.parse(runAt);
  if (!text) return NextResponse.json({ ok: false, error: "text_required" }, { status: 400 });
  if (!runAt || isNaN(ts)) return NextResponse.json({ ok: false, error: "bad_time" }, { status: 400 });
  const post = await addScheduled({ text, target, withButton, runAt: new Date(ts).toISOString() });
  return NextResponse.json({ ok: true, post });
}

export async function PATCH(req: NextRequest) {
  if (!authorized(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const body = await req.json().catch(() => null);
  const id = String(body?.id ?? "");
  const runAt = String(body?.runAt ?? "");
  const ts = Date.parse(runAt);
  if (!id) return NextResponse.json({ ok: false, error: "id_required" }, { status: 400 });
  if (!runAt || isNaN(ts)) return NextResponse.json({ ok: false, error: "bad_time" }, { status: 400 });
  const post = await rescheduleScheduled(id, new Date(ts).toISOString());
  if (!post) return NextResponse.json({ ok: false, error: "not_pending" }, { status: 409 });
  return NextResponse.json({ ok: true, post });
}

export async function DELETE(req: NextRequest) {
  if (!authorized(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const id = req.nextUrl.searchParams.get("id") || String((await req.json().catch(() => null))?.id ?? "");
  if (!id) return NextResponse.json({ ok: false, error: "id_required" }, { status: 400 });
  return NextResponse.json({ ok: await deleteScheduled(id) });
}
