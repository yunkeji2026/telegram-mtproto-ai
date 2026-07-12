// 共享路由元数据（无 "use client"：服务端 page 与客户端 client 都可安全引用）。
import type { ProductKey } from "@/lib/brand";

// 内部 view 键沿用旧名（liveavatar=视觉系 / soulsync=沟通系）：它们是漏斗埋点的核心维度，
// 改键名会断裂历史数据。对客可见层（tab 标签 / 标题 / 卡片）已全部无界化，键名仅作稳定标识。
export type View = "home" | "liveavatar" | "soulsync" | "pricing" | "engage";

/** 产品 → 所属 view 分组：幻境系(幻颜/幻声/幻影)=视觉(liveavatar)；
 *  智连系(智拓/智聊) + 通达系(通译/通传)=沟通(soulsync)。 */
export const PRODUCT_VIEW: Record<ProductKey, View> = {
  reachx: "soulsync",
  chatx: "soulsync",
  facex: "liveavatar",
  voicex: "liveavatar",
  livex: "liveavatar",
  lingox: "soulsync",
  voxx: "soulsync",
};

/** 入口别名 → 视图（兼容 ?view= 与 startapp start_param 的历史别名；含无界 5 产品 key）。 */
export const VIEW_ALIASES: Record<string, View> = {
  home: "home",
  overview: "home",
  contact: "home",
  // 视觉系（幻颜 / 幻声 / 幻影）
  liveavatar: "liveavatar",
  realtime: "liveavatar",
  faceswap: "liveavatar",
  voice: "liveavatar",
  "digital-human": "liveavatar",
  "video-dubbing": "liveavatar",
  facex: "liveavatar",
  voicex: "liveavatar",
  livex: "liveavatar",
  // 沟通系（智连：智拓/智聊 + 通达：通译/通传）
  soulsync: "soulsync",
  autochat: "soulsync",
  translate: "soulsync",
  interpret: "soulsync",
  chat: "soulsync",
  reach: "soulsync",
  reachx: "soulsync",
  chatx: "soulsync",
  lingox: "soulsync",
  voxx: "soulsync",
  pricing: "pricing",
  plans: "pricing",
  engage: "engage",
  deploy: "engage",
  invest: "engage",
};

export function resolveView(param?: string | null): View {
  if (param && VIEW_ALIASES[param]) return VIEW_ALIASES[param];
  return "home";
}

export const TABS: { id: View; icon: string; zh: string; en: string }[] = [
  { id: "home", icon: "🏠", zh: "概览", en: "Home" },
  { id: "liveavatar", icon: "🎭", zh: "视觉", en: "Visual" },
  { id: "soulsync", icon: "💬", zh: "智聊", en: "Chat" },
  { id: "pricing", icon: "💰", zh: "价格", en: "Pricing" },
  { id: "engage", icon: "🤝", zh: "合作", en: "Engage" },
];
