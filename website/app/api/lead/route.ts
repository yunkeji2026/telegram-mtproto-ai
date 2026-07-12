import { NextRequest, NextResponse } from "next/server";
import { appendLead, upsertLead, notifyAdminsOfLead, type LeadRecord } from "@/lib/lead-store";
import { verifyInitData } from "@/lib/verify-initdata";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function clean(v: unknown, max: number) {
  return String(v ?? "").trim().slice(0, max);
}

// Permissive server-side guard mirroring the client validator: blocks obvious
// junk while accepting email / @handle / t.me / phone / WeChat-style ids.
function plausibleContact(v: string): boolean {
  if (v.length < 4) return false;
  if (/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v)) return true; // email
  if (/^@?[a-zA-Z0-9_]{4,}$/.test(v)) return true; // handle
  if (/(?:t\.me\/|telegram\.me\/|wa\.me\/)/i.test(v)) return true; // link
  const digits = (v.match(/\d/g) || []).length;
  if (digits >= 6 && /^[+\d()\-\s]+$/.test(v)) return true; // phone
  if (v.length >= 5 && /[a-zA-Z\u4e00-\u9fa5]/.test(v)) return true; // id with letters
  return false;
}

async function followUpCustomer(rec: LeadRecord) {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  // only message customers we can trust (verified Mini App user)
  if (!token || rec.verified !== "verified" || !rec.tg_user_id) return;
  const site = process.env.NEXT_PUBLIC_SITE_URL || "https://ai26.sbs";
  const zh = rec.lang === "zh";
  const text = zh
    ? `👋 ${rec.name || "你好"}，已收到你的需求！\n\n我们会尽快联系你（约 5 分钟内）。期间你可以先看看 AI 自动成交聊天的演示与套餐 👇`
    : `👋 Hi ${rec.name || "there"}, we got your request!\n\nWe'll reach out shortly (~5 min). Meanwhile, check the AI auto-closing chat demo & plans 👇`;
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), 6000);
  try {
    await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: rec.tg_user_id,
        text,
        reply_markup: {
          inline_keyboard: [
            [{ text: zh ? "🤖 AI 成交聊天" : "🤖 AI closing", web_app: { url: `${site}/#autochat` } }],
            [{ text: zh ? "💰 套餐与价格" : "💰 Plans & pricing", web_app: { url: `${site}/#pricing` } }],
          ],
        },
      }),
      signal: ac.signal,
    });
  } catch {
    /* best-effort: customer may not have started the bot */
  } finally {
    clearTimeout(timer);
  }
}

export async function POST(req: NextRequest) {
  try {
    const data = await req.json();

    // honeypot: real users never fill this hidden field
    if (clean(data?.hp, 50)) {
      return NextResponse.json({ ok: true });
    }

    const contact = clean(data?.contact, 200);
    if (!contact) {
      return NextResponse.json({ ok: false, error: "contact_required" }, { status: 400 });
    }

    // Verify Mini App leads when initData is present; mark trust level.
    let verified = "";
    let tgUserId = clean(data?.tg_user_id, 20);
    const initData = clean(data?.initData, 4096);
    if (clean(data?.source, 16) === "miniapp" && initData) {
      const token = process.env.TELEGRAM_BOT_TOKEN || "";
      const v = verifyInitData(initData, token);
      verified = v.ok ? "verified" : "unverified";
      if (v.ok && v.userId) tgUserId = String(v.userId);
    }

    // Format guard for non-trusted (web) leads; verified Mini App users are exempt
    // since their contact is auto-derived from Telegram identity.
    if (verified !== "verified" && !plausibleContact(contact)) {
      return NextResponse.json({ ok: false, error: "contact_invalid" }, { status: 400 });
    }

    const rec: LeadRecord = {
      t: new Date().toISOString(),
      name: clean(data?.name, 80),
      contact,
      interest: clean(data?.interest, 80),
      message: clean(data?.message, 1000),
      lang: clean(data?.lang, 8),
      source: clean(data?.source, 16),
      verified,
      tg_user_id: tgUserId,
      path: clean(data?.path, 200),
      ref: clean(req.headers.get("referer"), 300),
      ua: clean(req.headers.get("user-agent"), 250),
      ip: clean(
        req.headers.get("x-forwarded-for")?.split(",")[0] ||
          req.headers.get("x-real-ip"),
        60
      ),
    };

    // upsert BEFORE appending to the audit log, so the lazy migration never
    // double-counts the current record.
    const { entry, isNew } = await upsertLead(rec);
    await appendLead(rec);
    // only ping admins for genuinely new leads (dedup avoids notification spam)
    if (isNew) await notifyAdminsOfLead(entry);
    await followUpCustomer(rec);

    return NextResponse.json({ ok: true });
  } catch {
    return NextResponse.json({ ok: false, error: "server_error" }, { status: 500 });
  }
}
