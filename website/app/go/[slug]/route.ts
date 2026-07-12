import { NextRequest, NextResponse } from "next/server";
import { resolveShortlink, STABLE_TOUCHPOINTS } from "@/lib/domains";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// 域名防封 · 短链跳转 /go/<slug>
// 统一分发入口：/go/cs（客服）、/go/channel（频道）、/go/voice（声音页）…
// - 外部触点（Telegram）→ 302 到稳定 t.me 链接（跨域名不失效）
// - 站内页 → 302 到相对路径（自动跟随当前访问域名，换镜像零改动）
// 换目标只改 lib/domains.ts 的 SHORTLINK_TARGETS 一处。
export async function GET(req: NextRequest, { params }: { params: { slug: string } }) {
  const target = resolveShortlink(params.slug);
  // 未知短码：回落到客服触点（绝不 404 把私域用户丢掉）
  const dest = target ?? STABLE_TOUCHPOINTS.contact;
  const url = dest.startsWith("http") ? dest : new URL(dest, req.url).toString();
  return NextResponse.redirect(url, 302);
}
