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

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    chromeLikeUserAgent,
    isWhatsappPlatform,
    isWhatsappUrl,
    shouldSpoofWhatsappUa,
    WHATSAPP_HOST_RE,
  };
}
