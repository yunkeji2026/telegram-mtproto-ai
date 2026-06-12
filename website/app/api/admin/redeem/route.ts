import { NextRequest, NextResponse } from "next/server";
import { redeemCode, listCodes, unlockCounts } from "@/lib/unlock-store";
import { requireAdmin } from "@/lib/admin-auth";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  if (!requireAdmin(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const [codes, counts] = await Promise.all([listCodes(), unlockCounts()]);
  return NextResponse.json({ ok: true, counts, codes });
}

export async function POST(req: NextRequest) {
  if (!requireAdmin(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const body = await req.json().catch(() => null);
  const code = String(body?.code ?? "").trim();
  if (!code) {
    return NextResponse.json({ ok: false, error: "code_required" }, { status: 400 });
  }
  const result = await redeemCode(code);
  if (!result.ok) {
    if (result.reason === "expired") {
      return NextResponse.json(
        { ok: false, error: "expired", expiresAt: result.rec?.expiresAt ?? null },
        { status: 410 },
      );
    }
    return NextResponse.json({ ok: false, error: "not_found" }, { status: 404 });
  }
  const rec = result.rec;
  return NextResponse.json({
    ok: true,
    alreadyRedeemed: result.alreadyRedeemed,
    code: rec.code,
    contact: rec.contact ?? null,
    name: rec.name ?? null,
    tg_user_id: rec.userId,
    issuedAt: rec.issuedAt,
    expiresAt: rec.expiresAt ?? null,
    redeemedAt: rec.redeemedAt ?? null,
  });
}
