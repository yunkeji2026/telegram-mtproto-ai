/** WhatsApp Web 内嵌：伪装 Chrome User-Agent（去掉 Electron 字样）。
 * 双模式：renderer 全局 + Node(main/单测) require。 */
"use strict";

const WHATSAPP_HOST_RE = /web\.whatsapp\.com|whatsapp\.com/i;

function chromeLikeUserAgent(chromeVersion) {
  const v = String(chromeVersion || "126.0.6478.183").trim() || "126.0.6478.183";
  return (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) " +
    "Chrome/" + v + " Safari/537.36"
  );
}

function isWhatsappPlatform(platform) {
  return String(platform || "").toLowerCase() === "whatsapp";
}

function isWhatsappUrl(url) {
  return WHATSAPP_HOST_RE.test(String(url || ""));
}

function shouldSpoofWhatsappUa(platform, url) {
  return isWhatsappPlatform(platform) || isWhatsappUrl(url);
}

// D2 多平台内嵌：以下平台的官方 web 端会拒载/降级含「Electron」字样的 UA，须伪装 Chrome。
// telegram web 用默认 UA 已验证可用 → 不动（零回归）；其余内嵌平台统一伪装。
const CHROME_UA_PLATFORMS = new Set(["whatsapp", "instagram", "messenger", "x", "zalo"]);
const CHROME_UA_HOST_RE =
  /web\.whatsapp\.com|whatsapp\.com|instagram\.com|messenger\.com|facebook\.com|x\.com|twitter\.com|zalo\.me/i;

function needsChromeUa(platform) {
  return CHROME_UA_PLATFORMS.has(String(platform || "").toLowerCase());
}

function urlNeedsChromeUa(url) {
  return CHROME_UA_HOST_RE.test(String(url || ""));
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    chromeLikeUserAgent,
    isWhatsappPlatform,
    isWhatsappUrl,
    shouldSpoofWhatsappUa,
    needsChromeUa,
    urlNeedsChromeUa,
    CHROME_UA_PLATFORMS,
    WHATSAPP_HOST_RE,
    CHROME_UA_HOST_RE,
  };
}
