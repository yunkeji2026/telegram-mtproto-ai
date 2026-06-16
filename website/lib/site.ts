export const SITE_URL = process.env.NEXT_PUBLIC_SITE_URL || "https://usdt2026.cc";

// 人工客服。注意：@handle 是 Telegram 平台实体，更名需在 Telegram 客户端/BotFather 同步操作，
// 这里仅做引用集中化，可用环境变量覆盖。
export const TELEGRAM_HANDLE = process.env.NEXT_PUBLIC_TELEGRAM_HANDLE || "ai_zkw";
export const TELEGRAM_DISPLAY = `@${TELEGRAM_HANDLE}`;
export const CONTACT_URL = `https://t.me/${TELEGRAM_HANDLE}`;

// 自助 Bot + Mini App
export const BOT_HANDLE = process.env.NEXT_PUBLIC_BOT_HANDLE || "tgzkw_bot";
export const BOT_URL = `https://t.me/${BOT_HANDLE}`;
// Mini App 深链（群/频道内用 url 按钮打开；如已在 BotFather 配置 Main Mini App 则直开小程序）
export const MINIAPP_URL = `https://t.me/${BOT_HANDLE}?startapp=autochat`;

// Telegram 频道（案例/动态沉淀）
export const TELEGRAM_CHANNEL = process.env.NEXT_PUBLIC_TELEGRAM_CHANNEL || "hykj7";
export const CHANNEL_URL = `https://t.me/${TELEGRAM_CHANNEL}`;

// Telegram 讨论组 / 群（互动 + 裂变拉新）
export const TELEGRAM_GROUP = process.env.NEXT_PUBLIC_TELEGRAM_GROUP || "hykjz";
export const GROUP_URL = `https://t.me/${TELEGRAM_GROUP}`;
