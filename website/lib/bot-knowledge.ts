import { content } from "./content";
import { SITE_URL } from "./site";

export type BotLang = "zh" | "en";

export function detectLang(code?: string | null): BotLang {
  return code?.toLowerCase().startsWith("zh") ? "zh" : "en";
}

/** Pick the grounding language from the user's message text.
 *  CJK → zh facts; otherwise en facts (easier for the model to translate from). */
export function detectKnowledgeLang(text: string): BotLang {
  return /[\u4e00-\u9fff]/.test(text) ? "zh" : "en";
}

function t(lang: BotLang) {
  return content[lang];
}

export function buildWelcome(lang: BotLang) {
  return lang === "zh"
    ? `👋 欢迎来到 <b>华灵科技 HuaLing Tech</b> —— 一站式 AI 技术服务

🤖 <b>AI 智能客服</b>：直接发消息问我，7×24 秒回（价格 / 方案 / 对接都能答）
👤 <b>人工客服</b>：需要真人就点下方「人工客服」

两大产品线：
· 🎭 <b>华影 LiveAvatar</b>：实时换脸换声 · 数字人 · 视频翻译配音（直播 / 连麦 / 视频通话）
· 💬 <b>灵犀 SoulSync</b>：AI 自动成交聊天 · 多语种拟人翻译 · AI 伴侣
· 🔐 底座：无审查私有部署，数据不出网

👇 点下方功能菜单，或直接发消息开聊`
    : `👋 Welcome to <b>HuaLing Tech</b> — one-stop AI technical services

🤖 <b>AI assistant</b>: just message me, 24/7 instant replies (pricing / solutions / onboarding)
👤 <b>Human support</b>: tap "Human support" below anytime

Two product lines:
· 🎭 <b>HuaYing · LiveAvatar</b>: real-time face & voice swap · digital human · video dubbing (live / video call)
· 💬 <b>LingXi · SoulSync</b>: AI auto-closing chat · human-like translation · AI companion
· 🔐 Base: uncensored private deployment, data stays off-net

👇 Tap the menu below, or just send me a message`;
}

export function buildServices(lang: BotLang) {
  const sols = t(lang).solutions;
  const lines = sols.map((s) => `· <b>${s.title}</b>\n  ${s.desc}`).join("\n\n");
  return lang === "zh"
    ? `📦 <b>六大业务能力</b>\n\n${lines}\n\n💡 详情见官网各板块演示`
    : `📦 <b>Six core solutions</b>\n\n${lines}\n\n💡 See live demos on the site`;
}

export function buildPricing(lang: BotLang) {
  const c = t(lang);
  const plans = c.plans.items
    .map((p) => `· <b>${p.name}</b> — ${p.priceMonthly} USDT/月`)
    .join("\n");
  const rt = c.realtime.plans
    .map((p) => `· <b>${p.name}</b> — ${p.price}`)
    .join("\n");
  const engage = c.engage.models.map((m) => `· <b>${m.name}</b> — ${m.price}`).join("\n");

  return lang === "zh"
    ? `💰 <b>价格速览</b>（USDT 结算）

<b>AI 成交聊天 · 月付</b>
${plans}

<b>实时换脸 · 一次性部署</b>
${rt}

<b>合作方式</b>
${engage}

📱 打开 Mini App 可查看完整价格表与 ROI 试算`
    : `💰 <b>Pricing overview</b> (USDT)

<b>AI auto-closing chat · monthly</b>
${plans}

<b>Real-time face swap · one-time deploy</b>
${rt}

<b>Engagement models</b>
${engage}

📱 Open the Mini App for full tables & ROI calculator`;
}

export function buildAutochat(lang: BotLang) {
  const a = t(lang).autochat;
  const feats = a.features.map((f) => `· <b>${f.title}</b> — ${f.desc}`).join("\n");
  return `🤖 <b>${a.title}</b>\n\n${a.subtitle.slice(0, 280)}…\n\n${feats}`;
}

export function buildDeploy(lang: BotLang) {
  const e = t(lang).engage;
  const models = e.models
    .map((m) => `<b>${m.badge} · ${m.name}</b>\n${m.tagline}\n${m.price}`)
    .join("\n\n");
  return lang === "zh"
    ? `🤝 <b>三种合作方式</b>\n\n${models}\n\n硬件归你、数据私有不出网，全程 USDT。`
    : `🤝 <b>Three engagement models</b>\n\n${models}\n\nYou own hardware, data stays private, USDT only.`;
}

export function buildContact(lang: BotLang) {
  const c = t(lang).contact;
  return lang === "zh"
    ? `📞 <b>联系下单</b>\n\n· Telegram 客服：${c.telegramHandle}\n· 结算：USDT（${c.networks}）\n· ${c.responseTime}\n\n也可以在 Mini App 底部直接提交留资表单。`
    : `📞 <b>Contact &amp; order</b>\n\n· Telegram: ${c.telegramHandle}\n· Settle in USDT (${c.networks})\n· ${c.responseTime}\n\nOr submit the lead form in the Mini App.`;
}

export function buildFaqList(lang: BotLang) {
  const items = t(lang).faq.items;
  return items.map((it, i) => ({ index: i, q: it.q }));
}

export function buildFaqAnswer(lang: BotLang, index: number) {
  const item = t(lang).faq.items[index];
  if (!item) return null;
  return `❓ <b>${item.q}</b>\n\n${item.a}`;
}

function keywordRules(lang: BotLang) {
  const zh = [
    { keys: ["换脸", "换声", "直播", "连麦", "视频通话"], fn: () => t(lang).realtime.subtitle.slice(0, 400) + "…" },
    { keys: ["成交", "翻译", "聊天", "聚合", "客服", "谷歌"], fn: () => buildAutochat(lang) },
    { keys: ["价格", "多少钱", "费用", "usdt", "套餐", "月付"], fn: () => buildPricing(lang) },
    { keys: ["部署", "私有", "托管", "交钥匙", "投资", "分红", "合作"], fn: () => buildDeploy(lang) },
    { keys: ["声音", "克隆", "配音", "tts"], fn: () => {
      const s = t(lang).solutions.find((x) => x.id === "voice");
      return s ? `🎙 <b>${s.title}</b>\n${s.desc}\n\n价格：${s.pricing.map((p) => `${p.plan} ${p.price}`).join(" · ")}` : buildServices(lang);
    }},
    { keys: ["人工", "客服", "联系", "下单"], fn: () => buildContact(lang) },
    { keys: ["业务", "服务", "能力"], fn: () => buildServices(lang) },
  ];
  const en = [
    { keys: ["face", "swap", "live", "stream", "voice"], fn: () => t(lang).realtime.subtitle.slice(0, 400) + "…" },
    { keys: ["chat", "translat", "clos", "aggregat", "google"], fn: () => buildAutochat(lang) },
    { keys: ["price", "cost", "usdt", "plan", "monthly"], fn: () => buildPricing(lang) },
    { keys: ["deploy", "private", "turnkey", "invest", "partner"], fn: () => buildDeploy(lang) },
    { keys: ["clone", "voice", "tts", "dub"], fn: () => {
      const s = t(lang).solutions.find((x) => x.id === "voice");
      return s ? `🎙 <b>${s.title}</b>\n${s.desc}` : buildServices(lang);
    }},
    { keys: ["human", "support", "contact", "order"], fn: () => buildContact(lang) },
    { keys: ["service", "solution"], fn: () => buildServices(lang) },
  ];
  return lang === "zh" ? zh : en;
}

export function matchFreeText(text: string, lang: BotLang): string | null {
  const lower = text.toLowerCase().trim();
  if (!lower || lower.length < 2) return null;

  // FAQ fuzzy: question contains user text or vice versa
  for (const [i, item] of t(lang).faq.items.entries()) {
    const q = item.q.toLowerCase();
    if (q.includes(lower) || lower.includes(q.slice(0, 6))) {
      return buildFaqAnswer(lang, i);
    }
  }

  for (const rule of keywordRules(lang)) {
    if (rule.keys.some((k) => lower.includes(k))) return rule.fn();
  }

  return null;
}

export function buildFallback(lang: BotLang) {
  return lang === "zh"
    ? `我没完全理解你的问题 🤔\n\n试试发：价格、换脸、AI成交、合作方式\n或点下方按钮打开 Mini App 查看详情`
    : `I didn't quite catch that 🤔\n\nTry: pricing, face swap, AI chat, engagement\nOr tap below to open the Mini App`;
}

/** Compact, grounded knowledge context for the LLM (real prices & facts). */
export function buildKnowledgeContext(lang: BotLang): string {
  const c = t(lang);
  const parts: string[] = [];

  parts.push(
    lang === "zh"
      ? "公司：华灵科技 HuaLing Tech。两大产品线：华影 LiveAvatar（数字形象/换脸/直播）、灵犀 SoulSync（AI自动成交聊天/实时翻译/AI伴侣）。主推产品：灵犀 SoulSync 驱动的 AI 自动成交聊天系统。结算：全程 USDT。"
      : "Company: HuaLing Tech. Two product lines: HuaYing LiveAvatar (digital avatar/face swap/live) and LingXi SoulSync (AI auto-closing chat/live translation/AI companion). Flagship: AI Auto-Closing Chat System powered by LingXi SoulSync. Settlement: USDT only."
  );

  parts.push(
    lang === "zh" ? "【AI 自动成交聊天】" : "[AI Auto-Closing Chat]"
  );
  parts.push(c.autochat.subtitle);
  c.autochat.features.forEach((f) => parts.push(`- ${f.title}: ${f.desc}`));
  parts.push(
    (lang === "zh" ? "套餐：" : "Plans: ") +
      c.plans.items
        .map((p) => `${p.name} ${p.priceMonthly} USDT/${lang === "zh" ? "月" : "mo"}（${p.features.join("、")}）`)
        .join("; ")
  );

  parts.push(lang === "zh" ? "【实时换脸换声 · 私有部署】" : "[Real-time Face/Voice Swap · Private Deploy]");
  parts.push(c.realtime.subtitle);
  parts.push(
    (lang === "zh" ? "部署套餐：" : "Deploy plans: ") +
      c.realtime.plans.map((p) => `${p.name} ${p.price}（${p.specs.join("、")}）`).join("; ")
  );
  parts.push((lang === "zh" ? "更多服务：" : "Extras: ") + c.realtime.extras.join("; "));

  parts.push(lang === "zh" ? "【六大能力】" : "[Six solutions]");
  c.solutions.forEach((s) =>
    parts.push(`- ${s.title}: ${s.desc} | ${s.pricing.map((p) => `${p.plan} ${p.price}`).join(", ")}`)
  );

  parts.push(lang === "zh" ? "【三种合作方式】" : "[Three engagement models]");
  c.engage.models.forEach((m) => parts.push(`- ${m.name}（${m.badge}）: ${m.tagline} | ${m.you} / ${m.we} | ${m.price}`));

  parts.push(lang === "zh" ? "【常见问题】" : "[FAQ]");
  c.faq.items.forEach((it) => parts.push(`Q: ${it.q}\nA: ${it.a}`));

  parts.push(lang === "zh" ? "联系：Telegram 客服 @ai_zkw；Bot @tgzkw_bot。" : "Contact: Telegram @ai_zkw; Bot @tgzkw_bot.");

  return parts.join("\n");
}

export function systemPrompt(lang: BotLang): string {
  const kb = buildKnowledgeContext(lang);
  return lang === "zh"
    ? `你是「华灵科技 HuaLing Tech」的专业 AI 售前客服（产品线：华影 LiveAvatar、灵犀 SoulSync）。只能根据下面提供的资料回答，不要编造价格、参数或承诺收益。

要求：
- 【语言镜像】务必用「用户最新一条消息所用的语言」作答：用户用西班牙语/葡萄牙语/阿拉伯语/泰语/英语等，就用同种语言地道、口语化地回复（像本地母语销售，不要翻译腔）。用户用中文则用简体中文。
- 口语、热情、专业，像真人销售，主动引导客户咨询或留资。
- 回答简洁（一般 2-5 句），可用要点。
- 涉及价格只用资料里的真实数字；资料没有的就说"具体可找客服按你的需求报价"。
- 适当推荐主推「AI 自动成交聊天系统」。
- 纯文本回复，不要使用 markdown 符号（如 * # 等）。
- 不讨论违法用途；强调私有部署、数据不出网、USDT 结算。
- 结尾可引导："想要方案/报价可以留个联系方式，或点菜单打开官网。"

资料：
${kb}`
    : `You are the professional AI pre-sales agent for "HuaLing Tech" (product lines: HuaYing LiveAvatar, LingXi SoulSync). Answer ONLY from the material below. Never invent prices, specs or guarantee returns.

Rules:
- [Language mirroring] ALWAYS reply in the SAME language as the user's latest message: if they write Spanish/Portuguese/Arabic/Thai/etc., reply fluently and idiomatically in that exact language (like a native salesperson, no translationese). If Chinese, reply in Simplified Chinese.
- Warm and professional like a real salesperson; guide the user to inquire or leave contact.
- Be concise (usually 2-5 sentences), bullets ok.
- Use only real numbers from the material; if missing, say "support can quote based on your needs".
- Promote the flagship "AI Auto-Closing Chat System" when relevant.
- Plain text only, no markdown symbols (no * # etc).
- No illegal use; emphasize private deployment, off-net data, USDT.
- End by guiding: "leave your contact for a plan/quote, or open the site from the menu."

Material:
${kb}`;
}

// Mini App opens the lightweight /app page (the full marketing site is too heavy
// for some Telegram webviews). /app has #pricing and #contact anchors.
export const WEBAPP_SECTIONS = {
  home: `${SITE_URL}/app`,
  realtime: `${SITE_URL}/app`,
  autochat: `${SITE_URL}/app`,
  pricing: `${SITE_URL}/app#pricing`,
  engage: `${SITE_URL}/app#pricing`,
  contact: `${SITE_URL}/app#contact`,
} as const;
