import type { Metadata } from "next";
import LegalShell from "@/components/LegalShell";
import { termsSections, termsTitle, LEGAL_UPDATED } from "@/lib/legal-content";

export const metadata: Metadata = {
  title: "Terms of Service · BOUNDLESS",
  description: "BOUNDLESS Terms of Service: scope, USDT settlement, acceptable use and disclaimer.",
  alternates: {
    canonical: "/en/terms",
    languages: { "zh-CN": "/terms", en: "/en/terms", "x-default": "/terms" },
  },
  robots: { index: true, follow: true },
};

export default function TermsPageEn() {
  return <LegalShell title={termsTitle} updated={LEGAL_UPDATED} sections={termsSections} />;
}
