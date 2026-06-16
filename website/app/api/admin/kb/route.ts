import { NextRequest, NextResponse } from "next/server";
import { appendKbEntry, deleteKbEntry, listKbEntries } from "@/lib/kb-extra";
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
  return NextResponse.json({ ok: true, entries: await listKbEntries() });
}

export async function POST(req: NextRequest) {
  if (!authorized(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const body = await req.json().catch(() => null);
  const q = String(body?.q ?? "").trim();
  const a = String(body?.a ?? "").trim();
  const lang = body?.lang ? String(body.lang).slice(0, 8) : undefined;
  if (!q || !a) {
    return NextResponse.json({ ok: false, error: "q_and_a_required" }, { status: 400 });
  }
  const entry = await appendKbEntry({ q, a, lang });
  return NextResponse.json({ ok: true, entry });
}

export async function DELETE(req: NextRequest) {
  if (!authorized(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const id =
    req.nextUrl.searchParams.get("id") ||
    String((await req.json().catch(() => null))?.id ?? "");
  if (!id) {
    return NextResponse.json({ ok: false, error: "id_required" }, { status: 400 });
  }
  const removed = await deleteKbEntry(id);
  return NextResponse.json({ ok: removed });
}
