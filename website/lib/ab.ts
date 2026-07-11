// 轻量 A/B 实验：本地随机分桶（50/50），localStorage 持久化保证同一访客口径一致。
// 曝光与点击都带 variant 打点（/api/track 原始事件），CTR = cta_click / ab_expose 按桶对比。
import { getLocal, setLocal } from "./safe-storage";
import { track } from "./track";

export type AbVariant = "a" | "b";

const KEY_PREFIX = "ab_";
const exposed = new Set<string>();

/** 取该实验的分桶（首次访问随机 50/50 并落盘）。SSR 阶段返回 "a"（对照组）。 */
export function abVariant(experiment: string): AbVariant {
  if (typeof window === "undefined") return "a";
  const key = KEY_PREFIX + experiment;
  const saved = getLocal(key);
  if (saved === "a" || saved === "b") return saved;
  const v: AbVariant = Math.random() < 0.5 ? "a" : "b";
  setLocal(key, v);
  return v;
}

/** 记一次曝光（每次会话每实验只记一次，避免刷屏）。 */
export function abExpose(experiment: string, variant: AbVariant) {
  if (exposed.has(experiment)) return;
  exposed.add(experiment);
  track("ab_expose", { experiment, variant });
}

/** Hero 主 CTA 文案实验：A=现行方案导向，B=演示钩子导向。 */
export const HERO_CTA_COPY: Record<AbVariant, { zh: string; en: string }> = {
  a: { zh: "咨询 AI 成交方案", en: "Get an AI closing plan" },
  b: { zh: "看 AI 当场成交演示", en: "Watch AI close a deal live" },
};
