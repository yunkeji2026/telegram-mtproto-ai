import { NextRequest } from "next/server";

// Key classification:
// - TELEGRAM_SETUP_KEY: high-privilege deploy/setup key (webhook setup, cron triggers).
// - ADMIN_KEY: day-to-day operations key for the dashboard/content endpoints.
// Runtime admin endpoints accept either (backward compatible); setup-only endpoints
// require the setup key. If ADMIN_KEY is unset, the setup key is used as fallback.

function extractKey(req: NextRequest): string | null {
  // Priority: httpOnly session cookie (dashboard) > header (cron/setup) > query (legacy).
  return (
    req.cookies.get("admin_session")?.value ||
    req.headers.get("x-setup-key") ||
    req.nextUrl.searchParams.get("key")
  );
}

export function requireAdmin(req: NextRequest): boolean {
  const setup = process.env.TELEGRAM_SETUP_KEY;
  const admin = process.env.ADMIN_KEY || setup;
  if (!setup && !admin) return false;
  const given = extractKey(req);
  if (!given) return false;
  return given === admin || given === setup;
}

export function requireSetup(req: NextRequest): boolean {
  const setup = process.env.TELEGRAM_SETUP_KEY;
  if (!setup) return false;
  return extractKey(req) === setup;
}
