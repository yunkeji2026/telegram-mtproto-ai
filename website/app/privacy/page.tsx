import type { Metadata } from "next";
import LegalShell from "@/components/LegalShell";
import { privacySections, privacyTitle, LEGAL_UPDATED } from "@/lib/legal-content";

export const metadata: Metadata = {
  title: "隐私政策 Privacy Policy · 华灵科技 HuaLing Tech",
  description: "华灵科技 HuaLing Tech 隐私政策：我们收集哪些数据、如何使用、第三方与你的权利。",
  alternates: {
    canonical: "/privacy",
    languages: { "zh-CN": "/privacy", en: "/en/privacy", "x-default": "/privacy" },
  },
  robots: { index: true, follow: true },
};

export default function PrivacyPage() {
  return <LegalShell title={privacyTitle} updated={LEGAL_UPDATED} sections={privacySections} />;
}
