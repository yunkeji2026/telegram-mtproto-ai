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
  // 无界品牌族 · 三大产品系（智连 / 幻境 / 通达），每系产品同系列命名：
  //   中文「字系 + 双字」（智拓/智聊 · 幻颜/幻声/幻影 · 通译/通传），英文统一 `…X` 后缀。
  //   category 指向所属产品系（见 CATEGORIES）；alt 为更自解释的渠道备选名。
  products: {
    // ── 智连系 · 社交增长（Growth）──
    reachx: {
      zh: "智拓",
      en: "ReachX",
      alt: "GrowthReach",
      emoji: "🎯",
      category: "growth",
      break: { zh: "触达与获客之界", en: "the reach barrier" },
      desc: {
        zh: "私域流量获取：真机多号自动加友、打招呼、引流进私域",
        en: "Private-traffic acquisition: multi-device auto add / greet / funnel-in",
      },
      skuIds: ["reach"],
    },
    chatx: {
      zh: "智聊",
      en: "ChatX",
      alt: "ChatHub",
      emoji: "💬",
      category: "growth",
      break: { zh: "沟通与成交之界", en: "the sales barrier" },
      desc: {
        zh: "AI 聊天翻译坐席：多平台承接、自动开发客户、推进成交",
        en: "AI chat & translation desk: omni-channel, auto-nurture, closes deals",
      },
      // 智聊能力在 content.ts::autochat / plans，不在 solutions SKU 列表中。
      skuIds: [],
    },
    // ── 幻境系 · 数字分身（Studio）──
    facex: {
      zh: "幻颜",
      en: "FaceX",
      alt: "FaceSwap",
      emoji: "🎭",
      category: "studio",
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
      category: "studio",
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
      category: "studio",
      break: { zh: "身份之界", en: "the identity barrier" },
      desc: {
        zh: "实时直播换脸换声 + 数字人：低延迟的百变分身",
        en: "Real-time live face/voice swap & digital human",
      },
      skuIds: ["digital-human", "video-dubbing"],
    },
    // ── 通达系 · 跨语沟通（Lingo）──
    lingox: {
      zh: "通译",
      en: "LingoX",
      alt: "LiveLingo",
      emoji: "🌐",
      category: "lingo",
      break: { zh: "语言之界", en: "the language barrier" },
      desc: {
        zh: "实时聊天翻译：多平台双向互译 + 术语一致 + 客户资产沉淀",
        en: "Real-time chat translation across platforms & languages",
      },
      skuIds: ["translate"],
    },
    voxx: {
      zh: "通传",
      en: "VoxX",
      alt: "LiveInterpret",
      emoji: "🎧",
      category: "lingo",
      break: { zh: "语言之界", en: "the language barrier" },
      desc: {
        zh: "同声传译：会议 / 直播语音实时口译 + 双语字幕",
        en: "Simultaneous interpreting for meetings & live + bilingual captions",
      },
      skuIds: ["interpret"],
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

// 三大产品系（category）：母品牌无界 → 智连 / 幻境 / 通达 三系 → 各系产品。
// 官网按系分组陈列（导航 + 产品矩阵均消费本定义，改一处全站生效）。
export const CATEGORIES = {
  growth: {
    zh: "智连",
    en: "Growth",
    tagline: { zh: "社交增长", en: "Social Growth" },
    desc: {
      zh: "私域获客 + AI 聊天翻译坐席：从加到人，到自动跟进成交",
      en: "Private-traffic acquisition + AI chat desk: from first touch to closed deal",
    },
    break: { zh: "沟通与成交之界", en: "the reach & sales barrier" },
  },
  studio: {
    zh: "幻境",
    en: "Studio",
    tagline: { zh: "数字分身", en: "Digital Avatar" },
    desc: {
      zh: "换脸 / 声音克隆 / 实时直播换脸换声 / 数字人",
      en: "Face swap / voice clone / live face-voice swap / digital human",
    },
    break: { zh: "容貌 · 声音 · 身份之界", en: "the face / voice / identity barrier" },
  },
  lingo: {
    zh: "通达",
    en: "Lingo",
    tagline: { zh: "跨语沟通", en: "Cross-lingual" },
    desc: {
      zh: "实时聊天翻译 + 同声传译：中英及多语无障碍",
      en: "Real-time chat translation + simultaneous interpreting",
    },
    break: { zh: "语言之界", en: "the language barrier" },
  },
} as const;

export type CategoryKey = keyof typeof CATEGORIES;

/** 产品系固定展示顺序（智连 → 幻境 → 通达）。 */
export const CATEGORY_ORDER: CategoryKey[] = ["growth", "studio", "lingo"];

/** 产品固定展示顺序（按系聚合：智连 → 幻境 → 通达）。 */
export const PRODUCT_ORDER: ProductKey[] = [
  "reachx",
  "chatx",
  "facex",
  "voicex",
  "livex",
  "lingox",
  "voxx",
];

/** 取某产品系下的产品（按 PRODUCT_ORDER 顺序）。 */
export function productsInCategory(cat: CategoryKey): ProductKey[] {
  return PRODUCT_ORDER.filter((k) => BRAND.products[k].category === cat);
}

/** "无界科技 BOUNDLESS" 这类中英组合写法。 */
export function brandFull(): string {
  return BRAND.company.full;
}

/** 产品中英组合：幻颜 FaceX / 智聊 ChatX。 */
export function productLabel(key: ProductKey, lang: BrandLang = "zh"): string {
  const p = BRAND.products[key];
  return lang === "zh" ? `${p.zh} ${p.en}` : `${p.en} (${p.zh})`;
}

/** 全线产品的结构化清单（emoji + 名称 + 一句话能力），按固定展示顺序。
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

/** 全线产品概述的纯文本块（每行 "· 🎭 幻颜 FaceX：AI 换脸…"），用于 bot 文案拼接。
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
