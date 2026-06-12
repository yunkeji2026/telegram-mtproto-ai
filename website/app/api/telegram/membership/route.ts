import { NextRequest, NextResponse } from "next/server";
import { verifyInitData } from "@/lib/verify-initdata";
import { TELEGRAM_CHANNEL, TELEGRAM_GROUP } from "@/lib/site";
import { issueCode } from "@/lib/unlock-store";
import { upsertLead, appendLead, notifyAdminsOfLead, type LeadRecord } from "@/lib/lead-store";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const JOINED = new Set(["creator", "administrator", "member", "restricted"]);

function parseUser(initData: string): { username?: string; first_name?: string; language_code?: string } {
  try {
    const u = JSON.parse(new URLSearchParams(initData).get("user") ?? "{}");
    return { username: u?.username, first_name: u?.first_name, language_code: u?.language_code };
  } catch {
    return {};
  }
}

async function isMember(token: string, chat: string, userId: number): Promise<boolean> {
  try {
    const res = await fetch(
      `https://api.telegram.org/bot${token}/getChatMember?chat_id=${encodeURIComponent(chat)}&user_id=${userId}`
    );
    const data = await res.json();
    if (!data?.ok) return false;
    const status = data.result?.status as string;
    if (status === "restricted") return data.result?.is_member === true;
    return JOINED.has(status);
  } catch {
    return false;
  }
}

export async function POST(req: NextRequest) {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) {
    return NextResponse.json({ ok: false, error: "not_configured" }, { status: 503 });
  }
  const body = await req.json().catch(() => null);
  const initData = String(body?.initData ?? "");
  const v = verifyInitData(initData, token);
  if (!v.ok || !v.userId) {
    return NextResponse.json({ ok: false, error: "unverified" }, { status: 401 });
  }

  const [channel, group] = await Promise.all([
    isMember(token, `@${TELEGRAM_CHANNEL}`, v.userId),
    isMember(token, `@${TELEGRAM_GROUP}`, v.userId),
  ]);

  if (!(channel && group)) {
    return NextResponse.json({ ok: true, channel, group, userId: v.userId });
  }

  // fully joined → issue a one-time code and record a high-intent lead
  const u = parseUser(initData);
  const contact = u.username ? `@${u.username}` : `tg:${v.userId}`;
  const code = await issueCode(v.userId, { contact, name: u.first_name, lang: u.language_code });

  const rec: LeadRecord = {
    t: new Date().toISOString(),
    name: u.first_name || "",
    contact,
    interest: "门控解锁高意向 / Unlock high-intent",
    message: `专属码 ${code.code}`,
    lang: u.language_code || "",
    source: "unlock",
    verified: "verified",
    tg_user_id: String(v.userId),
  };
  try {
    const { entry, isNew } = await upsertLead(rec);
    await appendLead(rec);
    if (isNew) await notifyAdminsOfLead(entry);
  } catch {
    /* best-effort */
  }

  return NextResponse.json({ ok: true, channel, group, userId: v.userId, code: code.code });
}
