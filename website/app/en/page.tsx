import type { Metadata } from "next";
import SiteHome from "@/components/SiteHome";

const LANGUAGES = { "zh-CN": "/", en: "/en", "x-default": "/" };

export const metadata: Metadata = {
  title: "HuaLing Tech · HuaYing LiveAvatar × LingXi SoulSync",
  description:
    "HuaLing Tech: two product lines. HuaYing LiveAvatar — real-time AI face & voice swap, digital humans, video dubbing. LingXi SoulSync — AI auto-closing chat, human-like multilingual translation, AI companion. Powered by the HuaLing Engine uncensored private-deployment base. Settled in USDT.",
  alternates: { canonical: "/en", languages: LANGUAGES },
  openGraph: {
    type: "website",
    url: "/en",
    title: "HuaLing Tech · HuaYing LiveAvatar × LingXi SoulSync",
    description:
      "HuaYing LiveAvatar: real-time face & voice swap, digital humans, video dubbing. LingXi SoulSync: AI auto-closing chat, human-like translation, AI companion. Private deployment, settled in USDT.",
    siteName: "HuaLing Tech",
  },
  twitter: {
    card: "summary_large_image",
    title: "HuaLing Tech · HuaYing LiveAvatar × LingXi SoulSync",
    description:
      "HuaYing LiveAvatar: face & voice swap / digital humans. LingXi SoulSync: AI auto-closing chat / human-like translation / AI companion. Private deployment, USDT.",
  },
};

export default function HomeEn() {
  return <SiteHome />;
}
