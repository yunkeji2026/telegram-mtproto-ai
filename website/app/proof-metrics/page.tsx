import type { Metadata } from "next";
import fs from "node:fs";
import path from "node:path";
import TrustMetricsPage, { type IndexData } from "@/components/TrustMetricsPage";

// 构建期读入指标产物，注入静态 HTML（数字可被爬虫索引、首屏无闪烁）。读失败 → null，
// 前端挂载后仍会 fetch /metrics/index.json 兜底。
function readMetrics(): IndexData | null {
  try {
    const p = path.join(process.cwd(), "public", "metrics", "index.json");
    return JSON.parse(fs.readFileSync(p, "utf-8")) as IndexData;
  } catch {
    return null;
  }
}

export const metadata: Metadata = {
  title: "可信指标 · 可复现质量门禁 | 无界科技 BOUNDLESS",
  description:
    "不是营销话术，是能跑出来的数字：人设一致性、危机安全召回、语音语种一致性、记忆抽取、意图识别等自动化门禁（run_eval）的真实结果，每条附样本数与复现命令。",
  keywords: ["AI质量评测", "危机安全", "人设一致性", "可复现门禁", "run_eval", "AI客服可信度"],
  alternates: {
    canonical: "/proof-metrics",
    languages: { "zh-CN": "/proof-metrics", en: "/en/proof-metrics", "x-default": "/proof-metrics" },
  },
  robots: { index: true, follow: true },
};

export default function Page() {
  return <TrustMetricsPage initial={readMetrics()} />;
}
