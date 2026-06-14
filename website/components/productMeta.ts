// 五产品的「展示元数据」单一来源：图标 + 首页内锚点。
// 纯文案/名称在 lib/brand.ts；这里只补 UI 层需要、又不该污染纯数据源的部分
// （lucide 图标是 React/UI 依赖，故不放进 brand.ts）。
// ProductMatrix 与 /brand 页共用本文件，避免「同一映射散落多份、改一处漏一处」。
import {
  ScanFace,
  AudioLines,
  Video,
  Languages,
  Bot,
  type LucideIcon,
} from "lucide-react";
import type { ProductKey } from "@/lib/brand";

export const PRODUCT_ICONS: Record<ProductKey, LucideIcon> = {
  facex: ScanFace,
  voicex: AudioLines,
  livex: Video,
  lingox: Languages,
  chatx: Bot,
};

// 每个产品在首页跳转到的现有 demo / 详情 section（均为已存在的真实锚点，
// 见 SectionNav：autochat / realtime / showcase）。避免坏锚点。
export const PRODUCT_ANCHOR: Record<ProductKey, string> = {
  facex: "#showcase",
  voicex: "#realtime",
  livex: "#realtime",
  lingox: "#autochat",
  chatx: "#autochat",
};
