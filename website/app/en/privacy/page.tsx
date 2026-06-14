import type { Metadata } from "next";
import LegalShell from "@/components/LegalShell";
import { privacySections, privacyTitle, LEGAL_UPDATED } from "@/lib/legal-content";

export const metadata: Metadata = {
  title: "Privacy Policy · BOUNDLESS",
  description: "BOUNDLESS Privacy Policy: what data we collect, how we use it, third parties and your rights.",
  alternates: {
    canonical: "/en/privacy",
    languages: { "zh-CN": "/privacy", en: "/en/privacy", "x-default": "/privacy" },
  },
  robots: { index: true, follow: true },
};

export default function PrivacyPageEn() {
  return <LegalShell title={privacyTitle} updated={LEGAL_UPDATED} sections={privacySections} />;
}
