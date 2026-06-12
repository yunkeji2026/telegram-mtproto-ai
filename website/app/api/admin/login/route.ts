import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const COOKIE = "admin_session";

export async function POST(req: NextRequest) {
  const setup = process.env.TELEGRAM_SETUP_KEY;
  const admin = process.env.ADMIN_KEY || setup;
  if (!setup && !admin) {
    return NextResponse.json({ error: "server not configured" }, { status: 500 });
  }

  let key = "";
  try {
    const body = await req.json();
    key = String(body?.key ?? "");
  } catch {
    // ignore malformed body
  }

  if (!key || (key !== admin && key !== setup)) {
    return NextResponse.json({ error: "invalid key" }, { status: 401 });
  }

  const res = NextResponse.json({ ok: true });
  res.cookies.set(COOKIE, key, {
    httpOnly: true,
    secure: true,
    sameSite: "lax",
    path: "/",
    maxAge: 60 * 60 * 12, // 12h session
  });
  return res;
}
