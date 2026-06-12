import { NextRequest, NextResponse } from "next/server";
import { addTemplate, deleteTemplate, listTemplates } from "@/lib/schedule-store";
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
  return NextResponse.json({ ok: true, templates: await listTemplates() });
}

export async function POST(req: NextRequest) {
  if (!authorized(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const body = await req.json().catch(() => null);
  const name = String(body?.name ?? "").trim();
  const text = String(body?.text ?? "").trim();
  const target = String(body?.target ?? "channel") as BroadcastTarget;
  const withButton = body?.withButton !== false;
  if (!name || !text) {
    return NextResponse.json({ ok: false, error: "name_and_text_required" }, { status: 400 });
  }
  const tpl = await addTemplate({ name, text, target, withButton });
  return NextResponse.json({ ok: true, template: tpl });
}

export async function DELETE(req: NextRequest) {
  if (!authorized(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const id = req.nextUrl.searchParams.get("id") || String((await req.json().catch(() => null))?.id ?? "");
  if (!id) return NextResponse.json({ ok: false, error: "id_required" }, { status: 400 });
  return NextResponse.json({ ok: await deleteTemplate(id) });
}
