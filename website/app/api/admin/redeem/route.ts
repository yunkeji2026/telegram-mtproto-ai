import { NextRequest, NextResponse } from "next/server";
import {
  redeemCode,
  listCodes,
  unlockCounts,
  extendCode,
  extendUnredeemed,
  voidExpired,
  deleteCode,
  redemptionStats,
} from "@/lib/unlock-store";
import { requireAdmin } from "@/lib/admin-auth";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  if (!requireAdmin(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const [codes, counts, stats] = await Promise.all([listCodes(), unlockCounts(), redemptionStats()]);
  return NextResponse.json({ ok: true, counts, codes, stats });
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

// Extend / revive code expiry. Body: { code, days } (single) or { scope:"unredeemed", days } (batch).
export async function PATCH(req: NextRequest) {
  if (!requireAdmin(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const body = await req.json().catch(() => null);
  const days = Number(body?.days ?? 7);
  if (body?.scope === "unredeemed") {
    const { extended } = await extendUnredeemed(days);
    return NextResponse.json({ ok: true, extended, counts: await unlockCounts() });
  }
  const code = String(body?.code ?? "").trim();
  if (!code) return NextResponse.json({ ok: false, error: "code_required" }, { status: 400 });
  const rec = await extendCode(code, days);
  if (!rec) return NextResponse.json({ ok: false, error: "not_extendable" }, { status: 409 });
  return NextResponse.json({ ok: true, code: rec.code, expiresAt: rec.expiresAt, counts: await unlockCounts() });
}

// Cleanup. Body/query: { scope:"expired" } (void all dead) or { code } (delete one).
export async function DELETE(req: NextRequest) {
  if (!requireAdmin(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const body = await req.json().catch(() => null);
  const scope = body?.scope ?? req.nextUrl.searchParams.get("scope");
  if (scope === "expired") {
    const { removed } = await voidExpired();
    return NextResponse.json({ ok: true, removed, counts: await unlockCounts() });
  }
  const code = String(body?.code ?? req.nextUrl.searchParams.get("code") ?? "").trim();
  if (!code) return NextResponse.json({ ok: false, error: "code_required" }, { status: 400 });
  const removed = await deleteCode(code);
  return NextResponse.json({ ok: removed, counts: await unlockCounts() });
}
