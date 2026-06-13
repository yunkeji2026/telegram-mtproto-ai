// Single source of truth for brand identity (华灵科技 HuaLing Tech).
// Change names / taglines / prefixes here — the rest of the app imports from this file.

export const BRAND = {
  company: {
    zh: "华灵科技",
    en: "HuaLing Tech",
    // 意境派副名（用于品牌叙事 / tagline，不做主名）
    aura: "AuraLing",
    full: "华灵科技 HuaLing Tech",
    logoChar: "灵",
    tagline: {
      zh: "灵动智能，华丽呈现",
      en: "Intelligence, gracefully delivered.",
    },
  },
  // 两大产品线（6 项能力收编进 华影 / 灵犀，私有部署归底座 华灵 Engine）
  products: {
    huaying: {
      zh: "华影",
      en: "LiveAvatar",
      alt: "VividMirror",
      tagline: {
        zh: "华丽分身，即刻登场",
        en: "Your face, live anywhere.",
      },
    },
    lingxi: {
      zh: "灵犀",
      en: "SoulSync",
      tagline: {
        zh: "心有灵犀，语之上",
        en: "Beyond Words, SoulSync.",
      },
    },
  },
  engine: {
    zh: "华灵 Engine",
    en: "HuaLing Engine",
  },
  // 解锁 / 折扣码前缀（原 HYKJ-）
  discountPrefix: "HL",
  // 语言偏好的 localStorage key（原 yt-lang）
  langStorageKey: "hl-lang",
} as const;

export type BrandLang = "zh" | "en";

/** "华灵科技 HuaLing Tech" 这类中英组合写法。 */
export function brandFull(): string {
  return BRAND.company.full;
}

/** 产品中英组合：华影 LiveAvatar / 灵犀 SoulSync。 */
export function productLabel(key: "huaying" | "lingxi", lang: BrandLang = "zh"): string {
  const p = BRAND.products[key];
  return lang === "zh" ? `${p.zh} ${p.en}` : `${p.en} (${p.zh})`;
}
