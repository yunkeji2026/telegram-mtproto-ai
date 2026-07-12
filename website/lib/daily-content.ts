import path from "path";
import { generateText } from "./deepseek";
import { buildKnowledgeContext } from "./bot-knowledge";

// Map each theme to a relevant product image (reuse the catalog assets).
const THEME_IMAGE = [
  "overview", // 行业趋势
  "translate", // 实战技巧 · AI 自动成交
  "digital-human", // 客户故事
  "faceswap", // 产品力 · 实时换脸换声
  "translate", // 出海获客 · 多语种翻译
  "voice", // 避坑指南 · 翻译对比
  "overview", // 社群福利
];

function imagePathForTheme(idx: number): string {
  const id = THEME_IMAGE[idx % THEME_IMAGE.length] ?? "overview";
  return path.join(process.cwd(), "public", "products", `prod-${id}.jpg`);
}

// Rotating daily themes (evergreen, product-grounded marketing — not fabricated news).
// Real-time web/news ingestion would need a search API key (see roadmap).
const THEMES = [
  "行业趋势 · AI 出海获客的最新打法",
  "实战技巧 · 用 AI 自动成交聊天把流量变订单",
  "客户故事 · 多语种 AI 成交的真实成果",
  "产品力 · 实时换脸换声在直播/视频通话的应用",
  "出海获客 · 多语种拟人翻译如何拿下海外客户",
  "避坑指南 · 传统翻译软件 vs AI 拟人翻译",
  "社群福利 · 关注频道进群解锁专属优惠",
];

export function listThemes(): string[] {
  return THEMES;
}

export function themeForToday(): { idx: number; theme: string } {
  const idx = new Date().getDay() % THEMES.length;
  return { idx, theme: THEMES[idx] };
}

function escapeHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** Generate a daily channel post for a given (or today's) theme. HTML-safe. */
export async function generateDailyPost(
  themeIdx?: number
): Promise<{ theme: string; text: string; imagePath: string } | null> {
  const picked =
    typeof themeIdx === "number"
      ? { idx: themeIdx % THEMES.length, theme: THEMES[themeIdx % THEMES.length] }
      : themeForToday();

  const knowledge = buildKnowledgeContext("zh");
  const system =
    `你是无界科技 BOUNDLESS 的资深社媒文案，负责官方 Telegram 频道（三大产品系：智连=智拓 ReachX·智聊 ChatX，幻境=幻颜 FaceX·幻声 VoiceX·幻影 LiveX，通达=通译 LingoX·通传 VoxX）。基于以下产品事实创作营销帖：\n${knowledge}\n\n` +
    `写作要求：\n` +
    `- 简体中文，口吻专业又有感染力，像顶尖出海营销号。\n` +
    `- 开头一行：emoji + 抓人标题。\n` +
    `- 中间 3-4 条要点，每行以 emoji 开头，短句、有冲击力。\n` +
    `- 结尾一句行动号召，引导私聊咨询 / 进群 / 打开小程序。\n` +
    `- 末尾 3-5 个相关话题标签（#开头）。\n` +
    `- 总长 120-220 字。不要使用 markdown 符号(* # \` )。不要编造具体数字、不存在的新闻或客户名。`;
  const user = `今天的选题：「${picked.theme}」。围绕这个选题写一条频道营销帖。`;

  const raw = await generateText(system, user);
  if (!raw) return null;
  return { theme: picked.theme, text: escapeHtml(raw.trim()), imagePath: imagePathForTheme(picked.idx) };
}
