import type { Metadata } from "next";
import SiteHome from "@/components/SiteHome";

const LANGUAGES = { "zh-CN": "/", en: "/en", "x-default": "/" };

export const metadata: Metadata = {
  title: "BOUNDLESS · Communication, Boundless",
  description:
    "BOUNDLESS: AI that breaks the barriers of face, voice and language — AI face swap, voice cloning, real-time live face & voice swap, live translation, and AI auto-closing chat. Uncensored private deployment, settled in USDT.",
  alternates: { canonical: "/en", languages: LANGUAGES },
  openGraph: {
    type: "website",
    url: "/en",
    title: "BOUNDLESS · Communication, Boundless",
    description:
      "AI face swap · voice cloning · real-time live face/voice swap · live translation · AI auto-closing chat. Private deployment, settled in USDT.",
    siteName: "BOUNDLESS",
  },
  twitter: {
    card: "summary_large_image",
    title: "BOUNDLESS · Communication, Boundless",
    description:
      "AI face swap · voice cloning · real-time live face/voice swap · live translation · AI auto-closing chat. Private deployment, USDT.",
  },
};

export default function HomeEn() {
  return <SiteHome />;
}
