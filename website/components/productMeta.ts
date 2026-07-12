// 五产品的「展示元数据」单一来源：图标路径 + 首页内锚点。
// 纯文案/名称在 lib/brand.ts；这里只补 UI 层需要、又不该污染纯数据源的部分。
// ProductMatrix / /brand 页 / 小程序首页共用本文件，避免「同一映射散落多份、改一处漏一处」。
import type { ProductKey } from "@/lib/brand";

// 五产品专属玻璃 3D 图标（透明底）：由 scripts/build-boundless-marks.ps1 从 {key}-white.png
// 抠白生成 public/brand/products/{key}.png（与主 ∞ 标识同一视觉语言）。
export const PRODUCT_IMG: Record<ProductKey, string> = {
  reachx: "/brand/products/chatx.png", // 待补专属图标：暂复用同系(智连) chatx 图，避免裂图
  chatx: "/brand/products/chatx.png",
  facex: "/brand/products/facex.png",
  voicex: "/brand/products/voicex.png",
  livex: "/brand/products/livex.png",
  lingox: "/brand/products/lingox.png",
  voxx: "/brand/products/lingox.png", // 待补专属图标：暂复用同系(通达) lingox 图
};

// 每个产品在首页跳转到的现有 demo / 详情 section（均为已存在的真实锚点，
// 见 SectionNav：autochat / realtime / showcase）。避免坏锚点。
export const PRODUCT_ANCHOR: Record<ProductKey, string> = {
  reachx: "#engage",
  chatx: "#autochat",
  facex: "#showcase",
  voicex: "#realtime",
  livex: "#realtime",
  lingox: "#autochat",
  voxx: "#realtime",
};

// 拥有独立落地页的产品线（zh 路径；en 为 /en 前缀）。矩阵卡片优先跳落地页，
// 没有落地页的产品仍回退到首页锚点。
export const PRODUCT_LANDING: Partial<Record<ProductKey, string>> = {
  voicex: "/voice",
  facex: "/face",
  livex: "/face",
  lingox: "/interpreting",
  voxx: "/interpreting", // 同传与实时翻译共用 interpreting 落地页
};
