import type { Metadata } from "next";
import LegalShell from "@/components/LegalShell";
import { termsSections, termsTitle, LEGAL_UPDATED } from "@/lib/legal-content";

export const metadata: Metadata = {
  title: "服务条款 Terms of Service · 华灵科技 HuaLing Tech",
  description: "华灵科技 HuaLing Tech 服务条款：服务范围、USDT 结算、使用规范与免责声明。",
  alternates: {
    canonical: "/terms",
    languages: { "zh-CN": "/terms", en: "/en/terms", "x-default": "/terms" },
  },
  robots: { index: true, follow: true },
};

export default function TermsPage() {
  return <LegalShell title={termsTitle} updated={LEGAL_UPDATED} sections={termsSections} />;
}
