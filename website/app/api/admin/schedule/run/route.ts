import { NextRequest, NextResponse } from "next/server";
import { runDuePosts } from "@/lib/schedule-store";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** Manual / external-cron trigger to flush due scheduled posts. */
export async function POST(req: NextRequest) {
  const key = process.env.TELEGRAM_SETUP_KEY;
  if (!key) return NextResponse.json({ ok: false, error: "not_configured" }, { status: 503 });
  const given = req.headers.get("x-setup-key") || req.nextUrl.searchParams.get("key");
  if (given !== key) return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  const r = await runDuePosts();
  return NextResponse.json({ ok: true, ...r });
}
