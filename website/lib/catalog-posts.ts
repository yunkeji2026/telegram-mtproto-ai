import path from "path";
import { content } from "./content";
import { SITE_URL } from "./site";

// Per-product emoji (keyed by solution id from content.ts)
const EMOJI: Record<string, string> = {
  voice: "🎙",
  faceswap: "🎭",
  translate: "💬",
  "private-ai": "🔐",
  "digital-human": "👤",
  "video-dubbing": "🎬",
};

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

export interface CatalogPost {
  id: string;
  title: string;
  text: string;
  image: string; // public URL (for web preview)
  imagePath: string; // local fs path (for multipart upload)
}

function imageFor(id: string): string {
  return `${SITE_URL}/products/prod-${id}.jpg`;
}

function imagePathFor(id: string): string {
  return path.join(process.cwd(), "public", "products", `prod-${id}.jpg`);
}

/** Build one channel post per product type, faithfully from the website content. */
export function buildCatalogPosts(lang: "zh" | "en" = "zh"): CatalogPost[] {
  const sols = content[lang].solutions;
  // flagship (highlight) first for marketing impact, rest keep source order
  const ordered = [...sols].sort((a, b) => Number(Boolean(b.highlight)) - Number(Boolean(a.highlight)));

  return ordered.map((s) => {
    const emoji = EMOJI[s.id] ?? "📦";
    const hot = s.highlight ? (lang === "zh" ? "🔥 旗舰主推\n\n" : "🔥 Flagship\n\n") : "";
    const feats = s.features.map((f) => `· ${esc(f)}`).join("\n");
    const prices = s.pricing
      .map((p) => `· ${esc(p.plan)} — ${esc(p.price)}${p.detail ? `（${esc(p.detail)}）` : ""}`)
      .join("\n");
    const capLabel = lang === "zh" ? "✨ 核心能力" : "✨ Capabilities";
    const priceLabel = lang === "zh" ? "💰 价格（USDT）" : "💰 Pricing (USDT)";
    const cta =
      lang === "zh"
        ? "👇 立即咨询，或点下方打开小程序查看完整方案"
        : "👇 Ask us now, or open the Mini App below for full details";

    const text =
      `${hot}${emoji} <b>${esc(s.title)}</b>\n` +
      `${esc(s.desc)}\n\n` +
      `${capLabel}\n${feats}\n\n` +
      `${priceLabel}\n${prices}\n\n` +
      `${cta}`;

    return { id: s.id, title: s.title, text, image: imageFor(s.id), imagePath: imagePathFor(s.id) };
  });
}

/** Build the pinned overview / directory post (image + product index). */
export function buildOverviewPost(lang: "zh" | "en" = "zh"): { caption: string; image: string; imagePath: string } {
  const posts = buildCatalogPosts(lang);
  const index = posts.map((p) => `· ${EMOJI[p.id] ?? "📦"} ${esc(p.title)}`).join("\n");
  const caption =
    lang === "zh"
      ? `✨ <b>无界科技 BOUNDLESS · 一站式 AI 技术服务</b>\n\n` +
        `从容貌、声音到语言、对话与私有部署，同一套技术栈按需组合。\n\n` +
        `<b>📦 核心能力</b>\n${index}\n\n` +
        `全程 USDT 结算 · 数据私有不出网 · 可私有定制\n\n` +
        `👇 点下方打开官网 / 小程序 / 联系客服`
      : `✨ <b>BOUNDLESS · One-stop AI technical services</b>\n\n` +
        `From face, voice to language, chat & private deployment — one stack, mix as you need.\n\n` +
        `<b>📦 Core capabilities</b>\n${index}\n\n` +
        `USDT only · data stays private · fully customizable\n\n` +
        `👇 Open the site / Mini App / contact us below`;
  return {
    caption,
    image: `${SITE_URL}/products/prod-overview.png`,
    imagePath: imagePathFor("overview"),
  };
}
