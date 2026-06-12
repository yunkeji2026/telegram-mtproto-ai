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

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: "华灵科技 HuaLing Tech · 华影 LiveAvatar × 灵犀 SoulSync",
  description:
    "华灵科技 HuaLing Tech：两大产品线。华影 LiveAvatar——实时换脸换声、数字人、视频翻译配音；灵犀 SoulSync——AI 自动成交聊天、多语种拟人翻译、AI 伴侣。由华灵 Engine 无审查私有部署底座支撑，全程 USDT 结算。HuaLing Tech: HuaYing LiveAvatar for digital faces, LingXi SoulSync for AI chat & translation.",
  keywords: [
    "华灵科技",
    "HuaLing Tech",
    "华影",
    "LiveAvatar",
    "灵犀",
    "SoulSync",
    "AI自动成交",
    "聊天聚合",
    "实时翻译",
    "拟人翻译",
    "AI换脸",
    "声音克隆",
    "数字人",
    "私有部署",
    "USDT",
  ],
  alternates: { canonical: "/" },
  openGraph: {
    type: "website",
    url: SITE_URL,
    title: "华灵科技 HuaLing Tech · 华影 LiveAvatar × 灵犀 SoulSync",
    description:
      "华影 LiveAvatar：实时换脸换声 · 数字人 · 视频配音；灵犀 SoulSync：AI 自动成交聊天 · 拟人翻译 · AI 伴侣。无审查私有部署，全程 USDT 结算。",
    siteName: "华灵科技 HuaLing Tech",
  },
  twitter: {
    card: "summary_large_image",
    title: "华灵科技 HuaLing Tech · 华影 LiveAvatar × 灵犀 SoulSync",
    description:
      "华影 LiveAvatar：换脸换声/数字人；灵犀 SoulSync：AI 自动成交/拟人翻译/AI 伴侣。私有部署，USDT 结算。",
  },
};

const jsonLd = {
  "@context": "https://schema.org",
  "@type": "Organization",
  name: "华灵科技 HuaLing Tech",
  url: SITE_URL,
  slogan: "灵动智能，华丽呈现 · Intelligence, gracefully delivered.",
  description:
    "HuaLing Tech: two product lines — HuaYing LiveAvatar (real-time face & voice swap, digital humans, video dubbing) and LingXi SoulSync (AI auto-closing chat, human-like live translation, AI companion), on the HuaLing Engine uncensored private-deployment base. Settled in USDT.",
  sameAs: [CONTACT_URL],
};

const serviceLd = {
  "@context": "https://schema.org",
  "@type": "Service",
  name: "HuaYing LiveAvatar — Real-time AI Face & Voice Swap (Private Deployment)",
  serviceType: "Private deployment service for real-time AI face swap and voice cloning",
  description:
    "HuaYing LiveAvatar: live-stream / video-call grade real-time AI face swap + voice cloning, privately deployed on your own hardware and tailored to your scenario. Data stays local, off the public net. Settled in USDT.",
  provider: { "@type": "Organization", name: "华灵科技 HuaLing Tech", url: SITE_URL },
  areaServed: "Global",
  url: `${SITE_URL}/#realtime`,
  offers: realtimeOffers.map(toSchemaOffer),
};

const autochatLd = {
  "@context": "https://schema.org",
  "@type": "Service",
  name: "LingXi SoulSync — AI Auto-Closing Chat System",
  serviceType: "Chat aggregation with human-like AI translation and AI auto-closing",
  description:
    "LingXi SoulSync: unify TG / LINE / WhatsApp / Messenger inboxes with human-like AI translation (native slang, local idioms), AI that follows up and closes sales 24/7, plus persona voice chat. Far beyond Google-style translation APIs. Settled in USDT.",
  provider: { "@type": "Organization", name: "华灵科技 HuaLing Tech", url: SITE_URL },
  areaServed: "Global",
  url: `${SITE_URL}/#autochat`,
  offers: autochatOffers.map(toSchemaOffer),
};

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
        <Script src="https://telegram.org/js/telegram-web-app.js" strategy="beforeInteractive" />
        <script
          type="application/ld+json"
          dangerouslySetInnerHTML={{ __html: JSON.stringify(jsonLd) }}
        />
        <script
          type="application/ld+json"
          dangerouslySetInnerHTML={{ __html: JSON.stringify(autochatLd) }}
        />
        <script
          type="application/ld+json"
          dangerouslySetInnerHTML={{ __html: JSON.stringify(serviceLd) }}
        />
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
