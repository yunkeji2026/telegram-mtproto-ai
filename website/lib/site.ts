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

// 桌面客户端（智聊 ChatX for Windows）下载。
export const DESKTOP_VERSION = process.env.NEXT_PUBLIC_DESKTOP_VERSION || "0.1.0";
export const DESKTOP_SIZE_MB = process.env.NEXT_PUBLIC_DESKTOP_SIZE_MB || "238";
// 默认走站内相对路径：安装包放 website/public/downloads/，随 website 部署上传，
// 由 Next 直接 serve（无需外部 CDN，部署即可下）。迁 CDN/对象存储时设
// NEXT_PUBLIC_DOWNLOAD_WIN_URL 覆盖为绝对 URL 即可，无需改代码。
export const DOWNLOAD_WIN_URL =
  process.env.NEXT_PUBLIC_DOWNLOAD_WIN_URL || `/downloads/ChatX-Setup-${DESKTOP_VERSION}.exe`;
// 下载链接是否站内相对路径（决定是否加 download 属性 / 是否新开标签页）。
export const DOWNLOAD_IS_INTERNAL = DOWNLOAD_WIN_URL.startsWith("/");
