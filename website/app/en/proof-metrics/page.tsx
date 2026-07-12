import type { Metadata } from "next";
import fs from "node:fs";
import path from "node:path";
import TrustMetricsPage, { type IndexData } from "@/components/TrustMetricsPage";

function readMetrics(): IndexData | null {
  try {
    const p = path.join(process.cwd(), "public", "metrics", "index.json");
    return JSON.parse(fs.readFileSync(p, "utf-8")) as IndexData;
  } catch {
    return null;
  }
}

export const metadata: Metadata = {
  title: "Trust Metrics · Reproducible Quality Gates | BOUNDLESS",
  description:
    "Not marketing claims — numbers you can run: real results of automated gates (run_eval) for persona consistency, crisis-safety recall, voice-language consistency, memory extraction, intent accuracy and more, each with sample counts and the command to reproduce.",
  keywords: ["AI quality eval", "crisis safety", "persona consistency", "reproducible gates", "run_eval", "AI trust"],
  alternates: {
    canonical: "/en/proof-metrics",
    languages: { "zh-CN": "/proof-metrics", en: "/en/proof-metrics", "x-default": "/proof-metrics" },
  },
  robots: { index: true, follow: true },
};

export default function Page() {
  return <TrustMetricsPage initial={readMetrics()} />;
}
