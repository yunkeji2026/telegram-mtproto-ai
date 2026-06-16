import { NextRequest, NextResponse } from "next/server";
import { buildCatalogPosts, buildOverviewPost } from "@/lib/catalog-posts";
import {
  broadcastPhoto,
  broadcastMessage,
  pinMessage,
  deleteMessage,
  targetChats,
  type BroadcastTarget,
} from "@/lib/tg-broadcast";
import { getCatalogRefs, saveCatalogRefs, type SentRef } from "@/lib/catalog-msg-store";
import { TELEGRAM_CHANNEL } from "@/lib/site";
import { requireAdmin } from "@/lib/admin-auth";
import { recordPublish } from "@/lib/publish-log";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const authorized = requireAdmin;

const delay = (ms: number) => new Promise((r) => setTimeout(r, ms));

export async function GET(req: NextRequest) {
  if (!authorized(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const lang = (req.nextUrl.searchParams.get("lang") as "zh" | "en") || "zh";
  return NextResponse.json({
    ok: true,
    overview: buildOverviewPost(lang),
    posts: buildCatalogPosts(lang),
  });
}

export async function POST(req: NextRequest) {
  if (!authorized(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const body = await req.json().catch(() => ({}));
  const target = (body?.target as BroadcastTarget) || "channel";
  const lang = (body?.lang as "zh" | "en") || "zh";
  const usePhoto = body?.photo !== false;
  const withOverview = body?.overview !== false;
  const pin = body?.pin !== false;

  // idempotent: delete previously published catalog messages first
  const prev = await getCatalogRefs();
  const toDelete = [...prev.posts, ...prev.overview];
  for (const ref of toDelete) {
    await deleteMessage(ref.chat, ref.messageId).catch(() => false);
  }

  const posts = buildCatalogPosts(lang);
  const newPostRefs: SentRef[] = [];
  const results: { id: string; ok: boolean; error?: string }[] = [];

  for (const p of posts) {
    const res = usePhoto
      ? await broadcastPhoto({ photo: p.imagePath, caption: p.text, target, withButton: true })
      : await broadcastMessage({ text: p.text, target, withButton: true });
    const bad = res.results.find((r) => !r.ok);
    results.push({ id: p.id, ok: res.ok, error: bad?.error });
    for (const r of res.results) {
      if (r.ok && r.messageId) newPostRefs.push({ chat: r.chat, messageId: r.messageId });
    }
    await delay(1500);
  }

  // overview post + pin (channel only for pinning)
  const newOverviewRefs: SentRef[] = [];
  if (withOverview) {
    const ov = buildOverviewPost(lang);
    const res = await broadcastPhoto({ photo: ov.imagePath, caption: ov.caption, target, withButton: true });
    results.push({ id: "overview", ok: res.ok, error: res.results.find((r) => !r.ok)?.error });
    for (const r of res.results) {
      if (r.ok && r.messageId) newOverviewRefs.push({ chat: r.chat, messageId: r.messageId });
    }
    if (pin) {
      const channelChat = `@${TELEGRAM_CHANNEL}`;
      const ref = newOverviewRefs.find((r) => r.chat === channelChat) ?? newOverviewRefs[0];
      if (ref) await pinMessage(ref.chat, ref.messageId, true).catch(() => false);
    }
  }

  await saveCatalogRefs(newPostRefs, newOverviewRefs);

  const allOk = results.every((r) => r.ok);
  if (results.some((r) => r.ok)) {
    await recordPublish({
      kind: "catalog",
      target,
      summary: `产品目录 ${posts.length} 条${withOverview ? " + 总览" : ""}`,
    });
  }

  return NextResponse.json({ ok: allOk, count: results.length, results });
}
