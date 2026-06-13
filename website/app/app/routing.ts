// 共享路由元数据（无 "use client"：服务端 page 与客户端 client 都可安全引用）。

export type View = "home" | "liveavatar" | "soulsync" | "pricing" | "engage";

/** 入口别名 → 视图（兼容 ?view= 与 startapp start_param 的历史别名）。 */
export const VIEW_ALIASES: Record<string, View> = {
  home: "home",
  overview: "home",
  contact: "home",
  liveavatar: "liveavatar",
  realtime: "liveavatar",
  faceswap: "liveavatar",
  voice: "liveavatar",
  "digital-human": "liveavatar",
  "video-dubbing": "liveavatar",
  soulsync: "soulsync",
  autochat: "soulsync",
  translate: "soulsync",
  chat: "soulsync",
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
  { id: "liveavatar", icon: "🎭", zh: "华影", en: "LiveAvatar" },
  { id: "soulsync", icon: "💬", zh: "灵犀", en: "SoulSync" },
  { id: "pricing", icon: "💰", zh: "价格", en: "Pricing" },
  { id: "engage", icon: "🤝", zh: "合作", en: "Engage" },
];
