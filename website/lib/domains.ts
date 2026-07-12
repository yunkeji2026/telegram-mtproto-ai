// 域名防封 · 单一事实源
// ───────────────────────────────────────────────────────────────────────────
// 私域站的命门是「域名被封就得立刻换」。这里集中管理：
//   ① 镜像域名池（每个域名部署同一套站，被封换下一个）
//   ② 当前部署的主域名（各镜像各自设置）
//   ③ 永不被封的稳定触点（Telegram 频道/群/客服/Bot）——用户走丢了回这里拿新地址
//   ④ 短链目的地表（/go/<slug> 统一分发，换目标只改这一处）
// 运营换域名/换客服号 => 只改环境变量或本文件，全站与短链自动跟随。
//
// 环境变量（各镜像部署时按需设置）：
//   NEXT_PUBLIC_MIRROR_DOMAINS = "a.cc,b.net,c.app"   镜像域名池（逗号分隔）
//   NEXT_PUBLIC_PRIMARY_DOMAIN = "a.cc"               本次部署的主域名

import { CONTACT_URL, CHANNEL_URL, GROUP_URL, BOT_URL, MINIAPP_URL } from "./site";

function parseList(v: string | undefined): string[] {
  return (v || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

/** 镜像域名池：被封换下一个。环境变量优先，回落已配置的备用域名。
 *  ⚠ 当前 3 个域名（Dynadot 注册）均解析到同一 IP 165.154.233.121：
 *     只能防「域名/DNS 级封禁」（换域名即可），防不了「服务器 IP 级封禁」（同 IP 一起挂）。
 *     真正抗封需把镜像分散到不同 IP/机房，或前置 Cloudflare 等 CDN（见实施05文档）。 */
export const MIRROR_DOMAINS: string[] = (() => {
  const fromEnv = parseList(process.env.NEXT_PUBLIC_MIRROR_DOMAINS);
  const fallback = ["ai26.sbs", "13x.lol", "aikf.lol"]; // 已做 DNS 跳转的备用域名（同 IP，见上）
  return Array.from(new Set(fromEnv.length ? fromEnv : fallback));
})();

/** 本次部署的主域名（各镜像部署时设不同值；回落域名池首个）。 */
export const PRIMARY_DOMAIN: string =
  (process.env.NEXT_PUBLIC_PRIMARY_DOMAIN || "").trim() || MIRROR_DOMAINS[0];

/** 永不被封的稳定触点：域名挂了，用户回 Telegram 拿最新地址。 */
export const STABLE_TOUCHPOINTS = {
  contact: CONTACT_URL,
  channel: CHANNEL_URL,
  group: GROUP_URL,
  bot: BOT_URL,
  miniapp: MINIAPP_URL,
} as const;

/** 短链目的地表：/go/<slug> 302 到这里。外部绝对 URL 直跳，站内相对路径跟随当前域名。 */
export const SHORTLINK_TARGETS: Record<string, string> = {
  // 稳定触点（跨域名不失效）
  cs: CONTACT_URL, // 客服
  channel: CHANNEL_URL, // 频道
  group: GROUP_URL, // 群
  bot: BOT_URL,
  app: MINIAPP_URL, // 小程序
  // 站内页（相对路径 → 自动跟随当前访问域名，换镜像零改动）
  home: "/",
  voice: "/voice",
  face: "/face",
  translate: "/interpreting",
};

/** 解析短码 → 目的地（未知返回 null）。 */
export function resolveShortlink(slug: string): string | null {
  return SHORTLINK_TARGETS[slug] ?? null;
}

/** 拼镜像绝对地址：mirrorUrl("b.net", "/voice") → "https://b.net/voice"。 */
export function mirrorUrl(domain: string, path = "/"): string {
  const p = path.startsWith("/") ? path : `/${path}`;
  return `https://${domain}${p}`;
}
