/* AI 客服工作台 PWA Service Worker（Phase 1）
 *
 * 策略（刻意保守，避免缓存到带鉴权的动态内容）：
 *  - 仅处理同源 GET。
 *  - /api/、/login、/logout 等动态/鉴权路径：完全不拦截，直连网络。
 *  - 页面导航（mode==navigate）：network-first，断网时回落离线壳，不缓存 HTML（杜绝陈旧鉴权页）。
 *  - /static/、/copilot/ 静态资源：stale-while-revalidate，加速二次加载且后台静默更新。
 */
"use strict";

const VERSION = "v3-2026-07-02";
const SHELL_CACHE = "ws-shell-" + VERSION;
const ASSET_CACHE = "ws-assets-" + VERSION;
const OFFLINE_URL = "/static/pwa/offline.html";
const PRECACHE = [OFFLINE_URL, "/static/pwa/icon.svg"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(SHELL_CACHE)
      .then((c) => c.addAll(PRECACHE))
      .then(() => self.skipWaiting())
      .catch(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(
        keys
          .filter((k) => k !== SHELL_CACHE && k !== ASSET_CACHE)
          .map((k) => caches.delete(k))
      );
      await self.clients.claim();
    })()
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  let url;
  try {
    url = new URL(req.url);
  } catch (_) {
    return;
  }
  if (url.origin !== self.location.origin) return;

  // 鉴权/动态：不拦截，避免缓存敏感内容或破坏登录态
  const p = url.pathname;
  if (
    p.startsWith("/api/") ||
    p.startsWith("/login") ||
    p.startsWith("/logout") ||
    p.startsWith("/ws") ||
    p.startsWith("/sse")
  ) {
    return;
  }

  // Range 请求（<audio>/<video> 拖动/续播）直连网络：让浏览器拿到正确的 206，
  // 媒体 seek 才正常；也避免把 206 塞进 Cache（Cache.put 不支持 206 → 会抛 TypeError）。
  if (req.headers.has("range")) return;

  // 页面导航：network-first，断网回落离线壳（不缓存 HTML）
  if (req.mode === "navigate") {
    event.respondWith(
      (async () => {
        try {
          return await fetch(req);
        } catch (_) {
          const cache = await caches.open(SHELL_CACHE);
          return (await cache.match(OFFLINE_URL)) || Response.error();
        }
      })()
    );
    return;
  }

  // 静态资源：stale-while-revalidate
  if (p.startsWith("/static/") || p.startsWith("/copilot/")) {
    event.respondWith(
      (async () => {
        const cache = await caches.open(ASSET_CACHE);
        const cached = await cache.match(req);
        const network = fetch(req)
          .then((res) => {
            // 仅缓存完整的 200（排除 206/分块、opaque、错误）；put 失败(配额等)静默降级不阻断
            if (res && res.status === 200) {
              cache.put(req, res.clone()).catch(() => {});
            }
            return res;
          })
          .catch(() => cached || Response.error());
        return cached || network;
      })()
    );
  }
});
