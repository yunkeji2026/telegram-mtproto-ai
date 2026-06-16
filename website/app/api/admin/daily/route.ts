import { NextRequest, NextResponse } from "next/server";
import { generateDailyPost } from "@/lib/daily-content";
import { addDraft, deleteDraft, listDrafts } from "@/lib/schedule-store";
import { broadcastMessage, broadcastPhoto, type BroadcastTarget } from "@/lib/tg-broadcast";
import { getAdminChats } from "@/lib/admin-store";
import { SITE_URL } from "@/lib/site";
import { requireAdmin } from "@/lib/admin-auth";
import { recordPublish } from "@/lib/publish-log";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const authorized = requireAdmin;

async function notifyAdmins(theme: string, text: string) {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) return;
  const chats = await getAdminChats();
  const msg = `📝 今日 AI 选题草稿（待审核）\n主题：${theme}\n\n${text}\n\n👉 到后台 ${SITE_URL}/admin 一键发布或编辑`;
  await Promise.allSettled(
    chats.map((chat) =>
      fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chat_id: chat, text: msg, parse_mode: "HTML", disable_web_page_preview: true }),
      })
    )
  );
}

export async function GET(req: NextRequest) {
  if (!process.env.TELEGRAM_SETUP_KEY) {
    return NextResponse.json({ ok: false, error: "not_configured" }, { status: 503 });
  }
  if (!authorized(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  return NextResponse.json({ ok: true, drafts: await listDrafts() });
}

export async function POST(req: NextRequest) {
  if (!authorized(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const body = await req.json().catch(() => ({}));
  const themeIdx = typeof body?.themeIdx === "number" ? body.themeIdx : undefined;
  const publish = body?.publish as BroadcastTarget | undefined;
  const notify = body?.notify === true;

  const post = await generateDailyPost(themeIdx);
  if (!post) {
    return NextResponse.json({ ok: false, error: "generation_failed" }, { status: 502 });
  }

  // auto-publish mode (DAILY_AUTO_PUBLISH=1 enables cron to publish directly)
  const autoEnv = process.env.DAILY_AUTO_PUBLISH === "1";
  const withImage = body?.image !== false; // default: attach a theme image
  if (publish && (autoEnv || !notify)) {
    const res = withImage
      ? await broadcastPhoto({ photo: post.imagePath, caption: post.text, target: publish, withButton: true })
      : await broadcastMessage({ text: post.text, target: publish, withButton: true });
    if (res.ok) await recordPublish({ kind: "daily", target: publish, summary: post.theme });
    return NextResponse.json({ ok: res.ok, published: true, theme: post.theme, results: res.results });
  }

  // default: store as draft for review (+ optionally ping admins)
  const draft = await addDraft({ text: post.text, theme: post.theme, source: "ai" });
  if (notify) await notifyAdmins(post.theme, post.text);
  return NextResponse.json({ ok: true, draft });
}

export async function DELETE(req: NextRequest) {
  if (!authorized(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const id = req.nextUrl.searchParams.get("id") || String((await req.json().catch(() => null))?.id ?? "");
  if (!id) return NextResponse.json({ ok: false, error: "id_required" }, { status: 400 });
  return NextResponse.json({ ok: await deleteDraft(id) });
}
