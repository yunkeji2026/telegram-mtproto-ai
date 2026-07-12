/** @type {import('next').NextConfig} */
// 注意：不设 X-Frame-Options / frame-ancestors 限制，Telegram Mini App 需要被 web.telegram.org iframe 承载。
const securityHeaders = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "X-DNS-Prefetch-Control", value: "on" },
  { key: "Permissions-Policy", value: "browsing-topics=(), interest-cohort=()" },
  // 私域站：全站响应级 noindex（覆盖 HTML/API/静态文件所有响应，强于 HTML <meta>，
  // 与 app/robots.ts(disallow) + layout metadata(robots) 三重保险）。不影响 Telegram iframe。
  { key: "X-Robots-Tag", value: "noindex, nofollow, noarchive" },
];

const nextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  compress: true,
  // 客户专属方案书是 public/proposal/ 下的自包含静态页；
  // 这里把干净 URL /proposal 与 /proposal/ 重写到该静态文件，方便直接发链接。
  async rewrites() {
    return [
      { source: "/proposal", destination: "/proposal/index.html" },
      { source: "/proposal/", destination: "/proposal/index.html" },
    ];
  },
  async headers() {
    return [
      {
        source: "/products/:path*",
        headers: [{ key: "Cache-Control", value: "public, max-age=31536000, immutable" }],
      },
      {
        source: "/showcase/:path*",
        headers: [{ key: "Cache-Control", value: "public, max-age=2592000" }],
      },
      {
        // 后台不入索引（robots.ts 已 disallow，这里再加响应头双保险）
        source: "/admin/:path*",
        headers: [{ key: "X-Robots-Tag", value: "noindex, nofollow" }],
      },
      {
        // 客户专属方案书为商业机密，不入搜索索引
        source: "/proposal/:path*",
        headers: [{ key: "X-Robots-Tag", value: "noindex, nofollow" }],
      },
      {
        source: "/:path*",
        headers: securityHeaders,
      },
    ];
  },
};

export default nextConfig;
