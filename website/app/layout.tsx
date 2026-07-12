import type { Metadata } from "next";
import Script from "next/script";
import "./globals.css";
import { LanguageProvider } from "@/components/LanguageContext";
import { TelegramProvider } from "@/components/TelegramProvider";
import GlobalChrome from "@/components/GlobalChrome";
import TgRedirect from "@/components/TgRedirect";
import { SITE_URL, CONTACT_URL } from "@/lib/site";
import { content } from "@/lib/content";
import { realtimeOffers, autochatOffers, toSchemaOffer } from "@/lib/pricing";
import { BRAND, PRODUCT_ORDER, type ProductKey } from "@/lib/brand";

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  // 私域站：禁止搜索引擎索引/收录（只做私域分发，不做公开推广/SEO）。
  // 与 app/robots.ts 的 disallow:"/" 双保险；被镜像域名部署时同样生效。
  robots: {
    index: false,
    follow: false,
    nocache: true,
    googleBot: { index: false, follow: false },
  },
  title: "无界科技 BOUNDLESS · 让沟通无界",
  description:
    "无界科技 BOUNDLESS：用 AI 打破容貌、声音、语言、沟通的边界。AI 换脸、声音克隆、实时直播换脸换声、实时换语言、AI 自动成交聊天，私有部署、数据不出网、全程 USDT 结算。BOUNDLESS: AI face swap, voice clone, real-time live face/voice swap, live translation, and AI auto-closing chat — privately deployed, settled in USDT.",
  keywords: [
    "无界科技",
    "BOUNDLESS",
    "AI换脸",
    "声音克隆",
    "实时换脸",
    "实时翻译",
    "AI自动成交",
    "聊天聚合",
    "数字人",
    "私有部署",
    "USDT",
    // 旧品牌词保留，承接更名期的搜索流量
    "华灵科技",
    "HuaLing Tech",
    "华影",
    "灵犀",
  ],
  alternates: {
    canonical: "/",
    languages: { "zh-CN": "/", en: "/en", "x-default": "/" },
  },
  openGraph: {
    type: "website",
    url: SITE_URL,
    title: "无界科技 BOUNDLESS · 让沟通无界",
    description:
      "AI 换脸 · 声音克隆 · 实时直播换脸换声 · 实时换语言 · AI 自动成交聊天。无审查私有部署，全程 USDT 结算。",
    siteName: "无界科技 BOUNDLESS",
  },
  twitter: {
    card: "summary_large_image",
    title: "无界科技 BOUNDLESS · 让沟通无界",
    description:
      "AI 换脸 · 声音克隆 · 实时直播换脸换声 · 实时换语言 · AI 自动成交聊天。私有部署，USDT 结算。",
  },
};

const jsonLd = {
  "@context": "https://schema.org",
  "@type": "Organization",
  name: "无界科技 BOUNDLESS",
  url: SITE_URL,
  slogan: "让沟通，无界 · Communication, Boundless.",
  description:
    "BOUNDLESS: an AI software company breaking the barriers of face, voice, language and communication — AI face swap, voice cloning, real-time live face/voice swap, live translation, and AI auto-closing chat, on an uncensored private-deployment base. Settled in USDT.",
  sameAs: [CONTACT_URL],
};

// 五产品结构化数据（Service）：名称/描述取自 lib/brand.ts 单一数据源。
// 仅已落地定价的 LiveX（实时换脸换声）/ ChatX（自动成交）挂 offers，其余先不挂价，
// 等对应产品定价上线再补。锚点均指向已存在的首页 section，避免坏链。
const PRODUCT_OFFERS: Partial<Record<ProductKey, Parameters<typeof toSchemaOffer>[0][]>> = {
  livex: realtimeOffers,
  chatx: autochatOffers,
};
const PRODUCT_SCHEMA_ANCHOR: Record<ProductKey, string> = {
  reachx: "#engage",
  chatx: "#autochat",
  facex: "#showcase",
  voicex: "#realtime",
  livex: "#realtime",
  lingox: "#autochat",
  voxx: "#realtime",
};
const productServices = PRODUCT_ORDER.map((key) => {
  const p = BRAND.products[key];
  const offers = PRODUCT_OFFERS[key];
  return {
    "@context": "https://schema.org",
    "@type": "Service",
    name: `${p.en} (${p.zh}) — ${p.desc.en}`,
    serviceType: p.desc.en,
    description: `${p.en}: ${p.desc.en}. Part of BOUNDLESS — breaking ${p.break.en}. Privately deployed on your own hardware, data stays off the public net, settled in USDT.`,
    provider: { "@type": "Organization", name: "无界科技 BOUNDLESS", url: SITE_URL },
    areaServed: "Global",
    url: `${SITE_URL}/${PRODUCT_SCHEMA_ANCHOR[key]}`,
    ...(offers ? { offers: offers.map(toSchemaOffer) } : {}),
  };
});

const faqLd = {
  "@context": "https://schema.org",
  "@type": "FAQPage",
  mainEntity: content.en.faq.items.map((it) => ({
    "@type": "Question",
    name: it.q,
    acceptedAnswer: { "@type": "Answer", text: it.a },
  })),
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        {/* Set <html lang> to match the route locale before hydration (no dynamic render cost).
            Static HTML defaults to zh-CN; this corrects /en* for screen readers & JS crawlers. */}
        <script
          dangerouslySetInnerHTML={{
            __html:
              "(function(){try{var p=location.pathname;document.documentElement.lang=(p==='/en'||p.indexOf('/en/')===0)?'en':'zh-CN';}catch(e){}})();",
          }}
        />
        <Script src="https://telegram.org/js/telegram-web-app.js" strategy="beforeInteractive" />
        <script
          type="application/ld+json"
          dangerouslySetInnerHTML={{ __html: JSON.stringify(jsonLd) }}
        />
        {productServices.map((svc) => (
          <script
            key={svc.name}
            type="application/ld+json"
            dangerouslySetInnerHTML={{ __html: JSON.stringify(svc) }}
          />
        ))}
        <script
          type="application/ld+json"
          dangerouslySetInnerHTML={{ __html: JSON.stringify(faqLd) }}
        />
        <TelegramProvider>
          <LanguageProvider>
            <TgRedirect />
            <GlobalChrome />
            {children}
          </LanguageProvider>
        </TelegramProvider>
      </body>
    </html>
  );
}
