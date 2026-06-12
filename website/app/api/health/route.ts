import { NextRequest, NextResponse } from "next/server";
import { gatherHealth } from "@/lib/health";
import { requireAdmin } from "@/lib/admin-auth";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  // deep checks (live external calls) are gated behind the admin key
  const deep = req.nextUrl.searchParams.get("deep") === "1" && requireAdmin(req);
  const h = await gatherHealth(deep);
  return NextResponse.json(h, { status: h.healthy ? 200 : 503 });
}
