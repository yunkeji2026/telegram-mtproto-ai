import type { Metadata } from "next";
import DownloadPage from "@/components/DownloadPage";

export const metadata: Metadata = {
  title: "Download ChatX Desktop · Multilingual AI Employee | BOUNDLESS",
  description:
    "Download ChatX for Windows: a bundled local service (no setup), add one AI key to enable translation, connect an account into the unified inbox, and let AI serve 24/7 in your persona. Free metered trial; customer data stays in a local database.",
  keywords: ["ChatX", "AI customer service", "chat translation", "multilingual AI", "cross-border", "AI employee", "free trial"],
  alternates: {
    canonical: "/en/download",
    languages: { "zh-CN": "/download", en: "/en/download", "x-default": "/download" },
  },
  robots: { index: true, follow: true },
};

export default function Page() {
  return <DownloadPage />;
}
