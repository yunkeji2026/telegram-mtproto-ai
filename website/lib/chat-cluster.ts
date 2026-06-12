// Lightweight, zero-cost clustering of chat questions for the admin dashboard.
// - Topic buckets via multilingual keyword regexes (no embeddings / no API cost).
// - Real language guessed from the question text itself (script + latin stopwords),
//   so it works on historical logs and is not limited to the stored zh/en field.

export interface ChatRec {
  q?: unknown;
  a?: unknown;
  lang?: unknown;
  source?: unknown;
  t?: unknown;
}

export interface TopicStat {
  id: string;
  label: string;
  count: number;
  samples: string[];
}

export interface UncoveredStat {
  q: string;
  count: number;
  lang: string;
}

export interface ClusterResult {
  total: number;
  topics: TopicStat[];
  langs: { lang: string; count: number }[];
  uncovered: UncoveredStat[];
  coverage: number; // % of questions matched to a known topic
}

// ── language guess (script first, then a few latin stopwords) ──
const LATIN_HINTS: { lang: string; re: RegExp }[] = [
  { lang: "es", re: /\b(hola|gracias|cuánto|cuanto|precio|qué|que|cómo|como|quiero|necesito|puede|para|tienen|servicio)\b/i },
  { lang: "pt", re: /\b(olá|ola|obrigado|quanto|preço|preco|você|voce|como|quero|preciso|serviço|servico|tem)\b/i },
  { lang: "fr", re: /\b(bonjour|merci|combien|prix|comment|je veux|besoin|service|vous|pour)\b/i },
  { lang: "de", re: /\b(hallo|danke|wie viel|preis|wie|ich möchte|brauche|dienst|sie)\b/i },
  { lang: "id", re: /\b(halo|terima kasih|berapa|harga|bagaimana|saya|mau|butuh|layanan)\b/i },
  { lang: "vi", re: /\b(xin chào|cảm ơn|bao nhiêu|giá|làm sao|tôi|muốn|cần|dịch vụ)\b/i },
];

export function guessLang(textRaw: string): string {
  const text = textRaw || "";
  if (/[\u4e00-\u9fff]/.test(text)) return "zh";
  if (/[\u3040-\u30ff]/.test(text)) return "ja";
  if (/[\uac00-\ud7af]/.test(text)) return "ko";
  if (/[\u0600-\u06ff]/.test(text)) return "ar";
  if (/[\u0e00-\u0e7f]/.test(text)) return "th";
  if (/[\u0400-\u04ff]/.test(text)) return "ru";
  if (/[\u0590-\u05ff]/.test(text)) return "he";
  for (const h of LATIN_HINTS) if (h.re.test(text)) return h.lang;
  return "en";
}

// ── topic buckets (first match wins) ──
const TOPICS: { id: string; label: string; re: RegExp }[] = [
  {
    id: "pricing",
    label: "价格 / 套餐",
    re: /价格|价钱|多少钱|费用|报价|套餐|收费|price|pricing|cost|how much|plan|precio|cu[aá]nto|quanto|pre[çc]o|سعر|تكلفة|ราคา|เท่าไหร่/i,
  },
  {
    id: "autochat",
    label: "AI 自动成交 / 翻译聚合",
    re: /自动成交|自动聊天|聚合|翻译|成交|拟人|auto|closing|aggregat|translat|chat system|conver|多语/i,
  },
  {
    id: "faceswap",
    label: "实时换脸 / 视频通话",
    re: /换脸|人脸|视频通话|直播|face\s?swap|face|video call|live|cara|rosto|الوجه|หน้า/i,
  },
  {
    id: "voice",
    label: "换声 / 声音克隆",
    re: /换声|声音|语音|配音|克隆声|voice|voz|clone|الصوت|เสียง/i,
  },
  {
    id: "deploy",
    label: "私有部署 / 安全",
    re: /私有部署|部署|本地|私有|服务器|安全|封号|风控|deploy|private|server|secure|safe|ban|seguro|privad|despliegue/i,
  },
  {
    id: "coop",
    label: "合作 / 投资分红 / 机房",
    re: /合作|投资|分红|佣金|代理|机房|托管|运维|turnkey|invest|commission|partner|hosting|datacenter|managed/i,
  },
  {
    id: "howto",
    label: "怎么用 / 接入流程",
    re: /怎么用|如何|怎样|流程|接入|对接|上手|教程|how to|how do|setup|integrat|c[oó]mo|como funciona|comment/i,
  },
  {
    id: "contact",
    label: "联系 / 试用 / 留资",
    re: /联系|客服|试用|演示|demo|trial|contact|联系方式|微信|telegram|whatsapp|留资|wechat/i,
  },
];

function normalize(s: string): string {
  return s.trim().toLowerCase().replace(/\s+/g, " ").slice(0, 120);
}

export function clusterChats(chatsRaw: ChatRec[]): ClusterResult {
  const chats = chatsRaw
    .map((c) => String(c.q ?? "").trim())
    .filter((q) => q.length > 0 && q.length <= 500);

  const total = chats.length;
  const topicCount: Record<string, number> = {};
  const topicSamples: Record<string, string[]> = {};
  const langCount: Record<string, number> = {};
  const uncoveredMap: Record<string, { q: string; count: number; lang: string }> = {};
  let matched = 0;

  for (const q of chats) {
    const lang = guessLang(q);
    langCount[lang] = (langCount[lang] ?? 0) + 1;

    const topic = TOPICS.find((t) => t.re.test(q));
    if (topic) {
      matched += 1;
      topicCount[topic.id] = (topicCount[topic.id] ?? 0) + 1;
      const arr = (topicSamples[topic.id] ??= []);
      if (arr.length < 3 && !arr.includes(q)) arr.push(q.slice(0, 80));
    } else {
      const key = normalize(q);
      const ex = uncoveredMap[key];
      if (ex) ex.count += 1;
      else uncoveredMap[key] = { q: q.slice(0, 120), count: 1, lang };
    }
  }

  const topics: TopicStat[] = TOPICS.map((t) => ({
    id: t.id,
    label: t.label,
    count: topicCount[t.id] ?? 0,
    samples: topicSamples[t.id] ?? [],
  }))
    .filter((t) => t.count > 0)
    .sort((a, b) => b.count - a.count);

  const langs = Object.entries(langCount)
    .map(([lang, count]) => ({ lang, count }))
    .sort((a, b) => b.count - a.count);

  const uncovered = Object.values(uncoveredMap)
    .sort((a, b) => b.count - a.count)
    .slice(0, 15);

  return {
    total,
    topics,
    langs,
    uncovered,
    coverage: total > 0 ? Number(((matched / total) * 100).toFixed(1)) : 0,
  };
}
