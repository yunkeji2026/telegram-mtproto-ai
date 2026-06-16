// 轻量前端语言识别（仅用于「实时翻译」体验的可视化提示，非翻译本身）。
// 思路：先按 Unicode 区段判非拉丁语种（可靠、零误判），拉丁字母再用高频功能词
// 启发式区分常见出海语言；都不命中则回退英语。粗粒度但稳，足够给访客「AI 已识别我的
// 语言、并会用它回复」的即时反馈，强化 LingoX 通译卖点。后端真正的语言镜像在
// bot-knowledge.ts::systemPrompt 的【语言镜像】指令里完成。
export type DetectedLang = { code: string; native: string; en: string };

const EMPTY: DetectedLang = { code: "", native: "", en: "" };

// 拉丁字母语言：靠功能词/特征字符判别（顺序敏感：先判特征更强的）。
const LATIN: { code: string; native: string; en: string; hint: RegExp }[] = [
  { code: "vi", native: "Tiếng Việt", en: "Vietnamese", hint: /[ăâđêôơư]|\b(giá|bao nhiêu|xin chào|cảm ơn|tôi muốn)\b/i },
  { code: "es", native: "Español", en: "Spanish", hint: /[ñ¿¡]|\b(hola|cuánto|cuesta|qué|cómo|gracias|precio|quiero|necesito|dónde|por favor)\b/i },
  { code: "pt", native: "Português", en: "Portuguese", hint: /[ãõç]|\b(olá|quanto|custa|preço|obrigad[oa]|você|quero|preciso|onde|como)\b/i },
  { code: "fr", native: "Français", en: "French", hint: /\b(bonjour|combien|prix|merci|je veux|comment|où|s'il vous plaît|coûte|coûtent)\b/i },
  { code: "de", native: "Deutsch", en: "German", hint: /[äöüß]|\b(hallo|wie viel|preis|danke|ich möchte|kosten|bitte)\b/i },
  { code: "id", native: "Bahasa", en: "Indonesian", hint: /\b(halo|berapa|harga|terima kasih|saya|mau|bagaimana|di mana)\b/i },
];

export function detectLang(text: string): DetectedLang {
  const t = (text || "").trim();
  if (!t) return EMPTY;
  // 非拉丁语种按字符区段判别（零误判）
  if (/[\u3040-\u309f\u30a0-\u30ff]/.test(t)) return { code: "ja", native: "日本語", en: "Japanese" };
  if (/[\uac00-\ud7af]/.test(t)) return { code: "ko", native: "한국어", en: "Korean" };
  if (/[\u0e00-\u0e7f]/.test(t)) return { code: "th", native: "ไทย", en: "Thai" };
  if (/[\u0600-\u06ff]/.test(t)) return { code: "ar", native: "العربية", en: "Arabic" };
  if (/[\u0400-\u04ff]/.test(t)) return { code: "ru", native: "Русский", en: "Russian" };
  if (/[\u4e00-\u9fff]/.test(t)) return { code: "zh", native: "中文", en: "Chinese" };
  // 拉丁：功能词启发式
  for (const l of LATIN) if (l.hint.test(t)) return { code: l.code, native: l.native, en: l.en };
  return { code: "en", native: "English", en: "English" };
}
