import type { MetadataRoute } from "next";

// 私域站：全站禁止搜索引擎抓取/索引（只做私域分发，不做公开推广/SEO）。
// 配合 app/layout.tsx 的 metadata.robots(noindex) 双保险；不暴露 sitemap。
export default function robots(): MetadataRoute.Robots {
  return {
    rules: { userAgent: "*", disallow: "/" },
  };
}
