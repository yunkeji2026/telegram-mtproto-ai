import { NextResponse } from "next/server";
import { PRIMARY_DOMAIN, MIRROR_DOMAINS, STABLE_TOUCHPOINTS } from "@/lib/domains";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// 域名防封 · 当前活跃域名查询
// 供 Telegram Bot / 短链服务 / 分发脚本调用：拿"现在该发哪个域名"。
// 一个域名被封时，运营改镜像部署的 NEXT_PUBLIC_PRIMARY_DOMAIN 环境变量重启，
// 分发侧查这个端点即可自动切到新地址；Telegram 触点永不变，是最终兜底。
export async function GET() {
  return NextResponse.json({
    ok: true,
    primary: PRIMARY_DOMAIN,
    primary_url: `https://${PRIMARY_DOMAIN}`,
    mirrors: MIRROR_DOMAINS,
    touchpoints: STABLE_TOUCHPOINTS,
    ts: Date.now(),
  });
}
