import {
  BotLang,
  detectKnowledgeLang,
  WEBAPP_SECTIONS,
  buildAutochat,
  buildContact,
  buildDeploy,
  buildFaqAnswer,
  buildFaqList,
  buildFallback,
  buildPricing,
  buildServices,
  buildWelcome,
  matchFreeText,
} from "./bot-knowledge";
import { CONTACT_URL, SITE_URL, CHANNEL_URL, GROUP_URL, BOT_URL, MINIAPP_URL } from "./site";
import { askDeepSeek } from "./deepseek";
import {
  appendLead,
  upsertLead,
  notifyAdminsOfLead,
  setLeadStatus,
  refreshLeadCard,
  STATUS_LABEL,
  type LeadStatus,
} from "./lead-store";

const INTENT = /价格|多少钱|报价|购买|下单|怎么收费|套餐|合作|定制|分红|投资|price|cost|buy|order|quote|pricing|plan|deploy|invest/i;

export type TgFrom = { id: number; username?: string; first_name?: string };

const API = (method: string) =>
  `https://api.telegram.org/bot${process.env.TELEGRAM_BOT_TOKEN}/${method}`;

type InlineBtn =
  | { text: string; web_app: { url: string } }
  | { text: string; url: string }
  | { text: string; callback_data: string };

export async function tgCall(method: string, body: Record<string, unknown>) {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) return null;
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), 8000);
  try {
    const res = await fetch(API(method), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: ac.signal,
    });
    return await res.json();
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

export function mainMenuKeyboard(lang: BotLang): InlineBtn[][] {
  const ai = lang === "zh" ? "🤖 AI 智能客服（问我）" : "🤖 AI assistant (ask me)";
  const open = lang === "zh" ? "🚀 打开官网" : "🚀 Open site";
  const rt = lang === "zh" ? "🎭 实时换脸" : "🎭 Live swap";
  const ac = lang === "zh" ? "💬 AI 成交" : "💬 AI closing";
  const pr = lang === "zh" ? "💰 价格" : "💰 Pricing";
  const eg = lang === "zh" ? "🤝 合作" : "🤝 Engage";
  const faq = lang === "zh" ? "❓ 常见问题" : "❓ FAQ";
  const human = lang === "zh" ? "👤 人工客服" : "👤 Human support";
  const ch = lang === "zh" ? "📢 官方频道" : "📢 Channel";
  const gr = lang === "zh" ? "💬 交流群" : "💬 Group";

  return [
    [{ text: ai, callback_data: "ask_ai" }],
    [{ text: open, web_app: { url: WEBAPP_SECTIONS.home } }],
    [
      { text: rt, web_app: { url: WEBAPP_SECTIONS.realtime } },
      { text: ac, web_app: { url: WEBAPP_SECTIONS.autochat } },
    ],
    [
      { text: pr, web_app: { url: WEBAPP_SECTIONS.pricing } },
      { text: eg, web_app: { url: WEBAPP_SECTIONS.engage } },
    ],
    [
      { text: ch, url: CHANNEL_URL },
      { text: gr, url: GROUP_URL },
    ],
    [
      { text: faq, callback_data: "faq_list" },
      { text: human, url: CONTACT_URL },
    ],
  ];
}

/** url-only keyboard usable in groups/channels (web_app buttons are invalid there).
 *  官网 / 小程序 / 机器人 / 客服 in a tidy 2×2 layout. */
export function richLinkKeyboard(lang: BotLang): InlineBtn[][] {
  const site = lang === "zh" ? "🌐 官网" : "🌐 Website";
  const app = lang === "zh" ? "📱 小程序" : "📱 Mini App";
  const bot = lang === "zh" ? "🤖 机器人" : "🤖 Bot";
  const human = lang === "zh" ? "👤 客服" : "👤 Support";
  return [
    [
      { text: site, url: SITE_URL },
      { text: app, url: MINIAPP_URL },
    ],
    [
      { text: bot, url: BOT_URL },
      { text: human, url: CONTACT_URL },
    ],
  ];
}

export function faqListKeyboard(lang: BotLang) {
  const items = buildFaqList(lang);
  const rows: InlineBtn[][] = [];
  for (let i = 0; i < items.length; i += 2) {
    const row: InlineBtn[] = [
      { text: items[i].q.slice(0, 32), callback_data: `faq:${items[i].index}` },
    ];
    if (items[i + 1]) {
      row.push({ text: items[i + 1].q.slice(0, 32), callback_data: `faq:${items[i + 1].index}` });
    }
    rows.push(row);
  }
  rows.push([
    { text: lang === "zh" ? "« 返回主菜单" : "« Main menu", callback_data: "menu" },
    { text: lang === "zh" ? "打开官网" : "Open site", web_app: { url: SITE_URL } },
  ]);
  return rows;
}

export async function sendText(
  chatId: number | string,
  text: string,
  keyboard?: InlineBtn[][],
  opts?: { plain?: boolean; replyTo?: number }
) {
  return tgCall("sendMessage", {
    chat_id: chatId,
    text,
    parse_mode: opts?.plain ? undefined : "HTML",
    disable_web_page_preview: true,
    reply_markup: keyboard ? { inline_keyboard: keyboard } : undefined,
    reply_to_message_id: opts?.replyTo,
    allow_sending_without_reply: true,
  });
}

export async function answerCallback(id: string, text?: string) {
  return tgCall("answerCallbackQuery", { callback_query_id: id, text });
}

const lastReply = new Map<number, number>();
const COOLDOWN_MS = 800;

function rateLimited(chatId: number) {
  const now = Date.now();
  const prev = lastReply.get(chatId) ?? 0;
  if (now - prev < COOLDOWN_MS) return true;
  lastReply.set(chatId, now);
  return false;
}

export async function handleCommand(
  chatId: number,
  cmd: string,
  lang: BotLang,
  startArg?: string
) {
  if (rateLimited(chatId)) return;

  const kb = mainMenuKeyboard(lang);

  if (startArg) {
    const sectionMap: Record<string, string> = {
      pricing: buildPricing(lang),
      autochat: buildAutochat(lang),
      realtime: buildServices(lang),
      engage: buildDeploy(lang),
      contact: buildContact(lang),
      faq: buildFaqList(lang).map((f) => f.q).join("\n"),
    };
    const msg = sectionMap[startArg] ?? buildWelcome(lang);
    await sendText(chatId, msg, kb);
    return;
  }

  const map: Record<string, string> = {
    start: buildWelcome(lang),
    help: buildWelcome(lang),
    services: buildServices(lang),
    pricing: buildPricing(lang),
    autochat: buildAutochat(lang),
    deploy: buildDeploy(lang),
    engage: buildDeploy(lang),
    contact: buildContact(lang),
    faq: lang === "zh" ? "👇 选一个常见问题：" : "👇 Pick a question:",
  };

  const key = cmd.replace(/^\//, "").split("@")[0].toLowerCase();
  if (key === "faq") {
    await sendText(chatId, map.faq, faqListKeyboard(lang));
    return;
  }
  const text = map[key] ?? buildWelcome(lang);
  await sendText(chatId, text, kb);
}

function quickLeadKeyboard(lang: BotLang): InlineBtn[][] {
  return [
    [{ text: lang === "zh" ? "📝 一键留资 · 客服联系我" : "📝 Quick lead · contact me", callback_data: "lead_quick" }],
    [
      { text: lang === "zh" ? "💰 套餐价格" : "💰 Pricing", web_app: { url: WEBAPP_SECTIONS.pricing } },
      { text: lang === "zh" ? "👤 人工客服" : "👤 Human", url: CONTACT_URL },
    ],
  ];
}

export async function handleFreeText(chatId: number, text: string, lang: BotLang) {
  if (rateLimited(chatId)) return;

  const intent = INTENT.test(text);
  // grounding follows the message language; output mirrors user's language via prompt
  const klang = detectKnowledgeLang(text);

  // 1) LLM grounded answer (DeepSeek); show typing while thinking
  await tgCall("sendChatAction", { chat_id: chatId, action: "typing" });
  const llm = await askDeepSeek(text, klang);
  if (llm) {
    await sendText(chatId, llm, intent ? quickLeadKeyboard(lang) : mainMenuKeyboard(lang), { plain: true });
    return;
  }

  // 2) deterministic keyword / FAQ fallback
  const reply = matchFreeText(text, klang) ?? buildFallback(klang);
  await sendText(chatId, reply, intent ? quickLeadKeyboard(lang) : mainMenuKeyboard(lang));
}

// group answering: stricter per-group cooldown to avoid spam
const lastGroupReply = new Map<number, number>();
const GROUP_COOLDOWN_MS = 4000;

function groupRateLimited(chatId: number) {
  const now = Date.now();
  const prev = lastGroupReply.get(chatId) ?? 0;
  if (now - prev < GROUP_COOLDOWN_MS) return true;
  lastGroupReply.set(chatId, now);
  return false;
}

/** Answer a question inside a group/supergroup. Triggered only on @mention /
 *  reply-to-bot / command (privacy mode stays ON). Always threads the reply and
 *  nudges to DM — never floods the group. */
export async function handleGroupMessage(
  chatId: number,
  text: string,
  lang: BotLang,
  replyTo?: number
) {
  if (groupRateLimited(chatId)) return;

  const links = richLinkKeyboard(lang);

  // bare @mention with no question → short intro, no model call
  if (!text.trim()) {
    const intro =
      lang === "zh"
        ? "👋 <b>华灵科技 HuaLing Tech AI 助手</b>在这\n🎭 华影 LiveAvatar：实时换脸换声 · 数字人　💬 灵犀 SoulSync：AI 自动成交 · 拟人翻译 · AI 伴侣 · 🔐 私有部署\n\n直接 @我提问，或点下方按钮 👇"
        : "👋 <b>HuaLing Tech AI assistant</b> here\n🎭 HuaYing LiveAvatar: live face/voice swap · digital human　💬 LingXi SoulSync: AI auto-closing · human-like translation · AI companion · 🔐 private deployment\n\n@mention me with a question, or tap below 👇";
    await sendText(chatId, intro, links, { replyTo });
    return;
  }

  const klang = detectKnowledgeLang(text);

  await tgCall("sendChatAction", { chat_id: chatId, action: "typing" });
  const llm = await askDeepSeek(text, klang);
  if (llm) {
    await sendText(chatId, llm, links, { plain: true, replyTo });
    return;
  }
  const reply = matchFreeText(text, klang) ?? buildFallback(klang);
  await sendText(chatId, reply, links, { replyTo });
}

export async function handleCallback(
  chatId: number,
  data: string,
  callbackId: string,
  lang: BotLang,
  from?: TgFrom,
  messageId?: number
) {
  // lead status update from admin notification buttons
  if (data.startsWith("lead:")) {
    const [, status, ...idParts] = data.split(":");
    const id = idParts.join(":");
    const valid: LeadStatus[] = ["contacted", "won", "lost"];
    if (valid.includes(status as LeadStatus) && id) {
      const entry = await setLeadStatus(id, status as LeadStatus);
      await answerCallback(callbackId, entry ? `已更新：${STATUS_LABEL[entry.status]}` : "未找到该留资");
      if (entry) {
        entry.notifyChat = chatId;
        entry.notifyMsg = messageId;
        await refreshLeadCard(entry);
      }
    } else {
      await answerCallback(callbackId);
    }
    return;
  }

  await answerCallback(callbackId);

  if (data === "lead_quick") {
    const contact = from?.username ? `@${from.username}` : `tg:${chatId}`;
    const rec = {
      t: new Date().toISOString(),
      name: from?.first_name || "",
      contact,
      interest: lang === "zh" ? "机器人高意向咨询" : "Bot high-intent",
      message: "",
      lang,
      source: "bot",
      verified: "verified",
      tg_user_id: String(chatId),
    };
    try {
      const { entry, isNew } = await upsertLead(rec);
      await appendLead(rec);
      if (isNew) await notifyAdminsOfLead(entry);
    } catch {
      /* best-effort */
    }
    await sendText(
      chatId,
      lang === "zh"
        ? "✅ 已记录！客服会尽快通过 Telegram 联系你（约 5 分钟内）。也可直接联系 " + CONTACT_URL
        : "✅ Got it! Support will reach you on Telegram shortly (~5 min). Or contact " + CONTACT_URL,
      mainMenuKeyboard(lang)
    );
    return;
  }

  if (data === "ask_ai") {
    await sendText(
      chatId,
      lang === "zh"
        ? "🤖 我就是 <b>AI 智能客服</b>，直接把问题发给我即可，比如：\n· 换脸怎么收费？\n· AI 成交聊天能接哪些平台？\n· 私有部署多少钱？\n\n需要真人随时点「👤 人工客服」。"
        : "🤖 I'm your <b>AI assistant</b> — just send me your question, e.g.:\n· How much is face swap?\n· Which platforms does AI closing support?\n· What's the price of private deployment?\n\nNeed a human? Tap \"👤 Human support\" anytime.",
      [[{ text: lang === "zh" ? "👤 人工客服" : "👤 Human support", url: CONTACT_URL }]]
    );
    return;
  }

  if (data === "menu") {
    await sendText(chatId, buildWelcome(lang), mainMenuKeyboard(lang));
    return;
  }
  if (data === "faq_list") {
    await sendText(
      chatId,
      lang === "zh" ? "👇 选一个常见问题：" : "👇 Pick a question:",
      faqListKeyboard(lang)
    );
    return;
  }
  if (data.startsWith("faq:")) {
    const idx = Number(data.slice(4));
    const ans = buildFaqAnswer(lang, idx);
    if (ans) {
      await sendText(chatId, ans, faqListKeyboard(lang));
    }
  }
}

export async function setupBot() {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) return { ok: false, error: "no token" };

  const secret = process.env.TELEGRAM_WEBHOOK_SECRET || "hualing-wh-" + token.slice(-8);
  const webhookUrl = `${SITE_URL}/api/telegram/webhook`;

  await tgCall("setWebhook", {
    url: webhookUrl,
    secret_token: secret,
    allowed_updates: ["message", "callback_query"],
    drop_pending_updates: true,
  });

  await tgCall("setMyCommands", {
    commands: [
      { command: "start", description: "主菜单 / Main menu" },
      { command: "services", description: "业务能力 · 华影&灵犀 / Solutions" },
      { command: "pricing", description: "价格 / Pricing" },
      { command: "autochat", description: "灵犀 · AI 自动成交聊天 / LingXi AI closing" },
      { command: "deploy", description: "合作方式 · 私有部署 / Engagement" },
      { command: "faq", description: "常见问题 / FAQ" },
      { command: "contact", description: "联系下单 / Contact" },
    ],
  });

  // Bot 身份（BotFather 级别）：名称 / 简介 / 关于。中文为默认，并设置英文 (en) 版本。
  await tgCall("setMyName", { name: "华灵科技 HuaLing Tech" });
  await tgCall("setMyName", { name: "HuaLing Tech", language_code: "en" });

  await tgCall("setMyShortDescription", {
    short_description:
      "华影 LiveAvatar 换脸换声·数字人 ｜ 灵犀 SoulSync AI成交·拟人翻译·AI伴侣。私有部署 · USDT 结算。",
  });
  await tgCall("setMyShortDescription", {
    short_description:
      "HuaYing LiveAvatar: face/voice swap & digital humans. LingXi SoulSync: AI closing, human-like translation & AI companion. Private deploy · USDT.",
    language_code: "en",
  });

  await tgCall("setMyDescription", {
    description:
      "华灵科技 HuaLing Tech —— 灵动智能，华丽呈现。\n\n" +
      "🎭 华影 LiveAvatar：实时换脸换声 · 数字人 · 视频翻译配音\n" +
      "💬 灵犀 SoulSync：AI 自动成交聊天 · 多语种拟人翻译 · AI 伴侣\n" +
      "🔐 华灵 Engine：无审查私有部署，数据不出网\n\n" +
      "全程 USDT 结算。点 /start 打开菜单，或直接发消息问我。",
  });
  await tgCall("setMyDescription", {
    description:
      "HuaLing Tech — Intelligence, gracefully delivered.\n\n" +
      "🎭 HuaYing LiveAvatar: real-time face & voice swap · digital humans · video dubbing\n" +
      "💬 LingXi SoulSync: AI auto-closing chat · human-like translation · AI companion\n" +
      "🔐 HuaLing Engine: uncensored private deployment, data stays off-net\n\n" +
      "Settled in USDT. Tap /start for the menu, or just message me.",
    language_code: "en",
  });

  await tgCall("setChatMenuButton", {
    menu_button: {
      type: "web_app",
      text: "华灵科技 · 小程序",
      web_app: { url: WEBAPP_SECTIONS.home },
    },
  });

  const me = await tgCall("getMe", {});
  return { ok: true, secret, webhookUrl, me };
}
