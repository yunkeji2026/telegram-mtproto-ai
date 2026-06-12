import { NextRequest, NextResponse } from "next/server";
import { broadcastMessage, type BroadcastTarget } from "@/lib/tg-broadcast";
import { requireAdmin } from "@/lib/admin-auth";
import { recordPublish } from "@/lib/publish-log";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const authorized = requireAdmin;

export async function POST(req: NextRequest) {
  if (!process.env.TELEGRAM_SETUP_KEY) {
    return NextResponse.json({ ok: false, error: "not_configured" }, { status: 503 });
  }
  if (!authorized(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  if (!process.env.TELEGRAM_BOT_TOKEN) {
    return NextResponse.json({ ok: false, error: "no_bot_token" }, { status: 503 });
  }

  const body = await req.json().catch(() => null);
  const text = String(body?.text ?? "").trim();
  const target = (String(body?.target ?? "channel") as BroadcastTarget);
  const withButton = body?.withButton !== false;
  if (!text) {
    return NextResponse.json({ ok: false, error: "text_required" }, { status: 400 });
  }

  const { ok, results } = await broadcastMessage({ text, target, withButton });
  if (ok) await recordPublish({ kind: "broadcast", target, summary: text });
  return NextResponse.json({ ok, results });
}
