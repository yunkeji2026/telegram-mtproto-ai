import type { MetadataRoute } from "next";
import { BRAND } from "@/lib/brand";

// PWA 清单：无界科技 BOUNDLESS。图标见 scripts/build-logo-lockups.ps1 生成的 pwa-*.png。
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: `${BRAND.company.full} · 让沟通无界`,
    short_name: BRAND.company.zh,
    description: `${BRAND.company.tagline.zh} —— 换脸 · 克隆声音 · 实时直播换脸换声 · 实时换语言 · AI 自动成交。私有部署，全程 USDT 结算。`,
    start_url: "/",
    display: "standalone",
    background_color: "#05060f",
    theme_color: "#05060f",
    icons: [
      {
        src: "/brand/logos/pwa-192.png",
        sizes: "192x192",
        type: "image/png",
        purpose: "any",
      },
      {
        src: "/brand/logos/pwa-512.png",
        sizes: "512x512",
        type: "image/png",
        purpose: "any",
      },
      {
        src: "/brand/logos/pwa-512.png",
        sizes: "512x512",
        type: "image/png",
        purpose: "maskable",
      },
    ],
  };
}
