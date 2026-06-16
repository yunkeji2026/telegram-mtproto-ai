// Single source of truth for brand identity (无界科技 BOUNDLESS).
// Change names / taglines / products here — the rest of the app imports from this file.

export const BRAND = {
  company: {
    zh: "无界科技",
    en: "BOUNDLESS",
    full: "无界科技 BOUNDLESS",
    logoChar: "界",
    tagline: {
      zh: "让沟通，无界",
      en: "Communication, Boundless.",
    },
  },
  // 五大子产品（无界品牌族）：每个打破一种「界」。
  // 英文主名走统一 `…X` 系列（X = 突破边界 / 无限变换）；alt 为更自解释的渠道备选名。
  products: {
    facex: {
      zh: "幻颜",
      en: "FaceX",
      alt: "FaceSwap",
      emoji: "🎭",
      break: { zh: "容貌之界", en: "the face barrier" },
      desc: {
        zh: "AI 换脸：图片 / 视频里随心变幻容貌",
        en: "AI face swap for images & video",
      },
      // 对应 content.ts::solutions 的底层 SKU id（产品↔SKU 映射单一来源）。
      skuIds: ["faceswap"],
    },
    voicex: {
      zh: "幻声",
      en: "VoiceX",
      alt: "VoiceClone",
      emoji: "🎙",
      break: { zh: "声音之界", en: "the voice barrier" },
      desc: {
        zh: "AI 声音克隆：惟妙惟肖的配音与语音合成",
        en: "Clone any voice for lifelike dubbing",
      },
      skuIds: ["voice"],
    },
    livex: {
      zh: "幻影",
      en: "LiveX",
      alt: "LiveMorph",
      emoji: "🎬",
      break: { zh: "身份之界", en: "the identity barrier" },
      desc: {
        zh: "实时直播换脸换声：低延迟的百变分身",
        en: "Real-time face & voice swap for live",
      },
      skuIds: ["digital-human", "video-dubbing"],
    },
    lingox: {
      zh: "通译",
      en: "LingoX",
      alt: "LiveLingo",
      emoji: "🌐",
      break: { zh: "语言之界", en: "the language barrier" },
      desc: {
        zh: "实时换语言：语音 + 文字同声互译",
        en: "Real-time translation across languages",
      },
      skuIds: ["translate"],
    },
    chatx: {
      zh: "智聊",
      en: "ChatX",
      alt: "ChatHub",
      emoji: "💬",
      break: { zh: "沟通与成交之界", en: "the sales barrier" },
      desc: {
        zh: "聚合 AI 聊天：全程自动开发客户、推进成交",
        en: "Omni-channel AI chat that closes deals",
      },
      // 智聊能力在 content.ts::autochat / plans，不在 solutions SKU 列表中。
      skuIds: [],
    },
  },
  engine: {
    zh: "无界底座",
    en: "BOUNDLESS Engine",
  },
  // 解锁 / 折扣码前缀
  discountPrefix: "BL",
  // 语言偏好的 localStorage key（沿用旧 key，避免老用户语言偏好丢失）
  langStorageKey: "hl-lang",
} as const;

export type BrandLang = "zh" | "en";
export type ProductKey = keyof typeof BRAND.products;

/** 五产品的固定展示顺序（打破容貌→声音→身份→语言→成交 五界）。 */
export const PRODUCT_ORDER: ProductKey[] = ["facex", "voicex", "livex", "lingox", "chatx"];

/** "无界科技 BOUNDLESS" 这类中英组合写法。 */
export function brandFull(): string {
  return BRAND.company.full;
}

/** 产品中英组合：幻颜 FaceX / 智聊 ChatX。 */
export function productLabel(key: ProductKey, lang: BrandLang = "zh"): string {
  const p = BRAND.products[key];
  return lang === "zh" ? `${p.zh} ${p.en}` : `${p.en} (${p.zh})`;
}

/** 五产品的结构化清单（emoji + 名称 + 一句话能力），按固定展示顺序。
 *  欢迎语 / bot 知识库 / system prompt / 营销帖等"产品线概述"统一消费这一份，
 *  避免同一段产品介绍散落多个文件、改一处漏五处。 */
export function productLineItems(lang: BrandLang) {
  return PRODUCT_ORDER.map((k) => {
    const p = BRAND.products[k];
    return {
      key: k,
      emoji: p.emoji,
      name: productLabel(k, lang),
      desc: p.desc[lang],
    };
  });
}

/** 五产品概述的纯文本块（每行 "· 🎭 幻颜 FaceX：AI 换脸…"），用于 bot 文案拼接。
 *  bullet 默认 "· "，html=true 时名称用 <b> 包裹（Telegram HTML parse_mode）。 */
export function productLinesText(lang: BrandLang, opts?: { bullet?: string; html?: boolean }): string {
  const bullet = opts?.bullet ?? "· ";
  const sep = lang === "zh" ? "：" : ": ";
  return productLineItems(lang)
    .map((it) => {
      const name = opts?.html ? `<b>${it.name}</b>` : it.name;
      return `${bullet}${it.emoji} ${name}${sep}${it.desc}`;
    })
    .join("\n");
}
