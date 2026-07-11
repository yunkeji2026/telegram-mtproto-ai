import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const COOKIE = "admin_session";

// 后台登录暴力破解防护：单 IP 滑动窗口(10 分钟内最多 8 次失败)。单实例内存态即可。
const WINDOW_MS = 10 * 60 * 1000;
const MAX_FAILS = 8;
const fails = new Map<string, number[]>();

function clientIp(req: NextRequest): string {
  const xff = req.headers.get("x-forwarded-for");
  if (xff) return xff.split(",")[0].trim();
  return req.headers.get("x-real-ip") || "unknown";
}

function tooMany(ip: string): boolean {
  const now = Date.now();
  const arr = (fails.get(ip) || []).filter((t) => now - t < WINDOW_MS);
  fails.set(ip, arr);
  return arr.length >= MAX_FAILS;
}

function recordFail(ip: string) {
  const now = Date.now();
  const arr = (fails.get(ip) || []).filter((t) => now - t < WINDOW_MS);
  arr.push(now);
  fails.set(ip, arr);
  // 轻量清理，防内存无限增长
  if (fails.size > 5000) {
    for (const [k, v] of fails) {
      if (v.every((t) => now - t >= WINDOW_MS)) fails.delete(k);
    }
  }
}

export async function POST(req: NextRequest) {
  const setup = process.env.TELEGRAM_SETUP_KEY;
  const admin = process.env.ADMIN_KEY || setup;
  if (!setup && !admin) {
    return NextResponse.json({ error: "server not configured" }, { status: 500 });
  }

  const ip = clientIp(req);
  if (tooMany(ip)) {
    return NextResponse.json(
      { error: "too many attempts, try again later" },
      { status: 429, headers: { "Retry-After": "600" } },
    );
  }

  let key = "";
  try {
    const body = await req.json();
    key = String(body?.key ?? "");
  } catch {
    // ignore malformed body
  }

  if (!key || (key !== admin && key !== setup)) {
    recordFail(ip);
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
