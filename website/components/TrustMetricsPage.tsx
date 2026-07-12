"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  ShieldCheck,
  Home,
  Languages,
  Send,
  Check,
  Terminal,
  BadgeCheck,
  HeartPulse,
} from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import BrandMark from "./BrandMark";
import Footer from "./Footer";
import { BRAND } from "@/lib/brand";
import { CONTACT_URL } from "@/lib/site";
import { track } from "@/lib/track";

/** 可信指标对外页（/proof-metrics、/en/proof-metrics）。
 *  数据来自 scripts/gen-trust-metrics.py 产出的 /metrics/index.json（run_eval 真实门禁结果）。
 *  诚实原则：只展示 status=pass 且带 headline 的确定性门禁；每条附样本数 + 复现命令。
 *  这是竞品给不出的信任资产——不是营销话术，是可当场复现的硬门禁。 */

type L = { zh: string; en: string };
type Headline = { metric: L; value: string; sample?: number };
type EvalEntry = {
  key: string;
  label: L;
  status: string;
  file: string;
  headline?: Headline;
};
export type IndexData = {
  generated_at: string;
  counts: { pass: number; fail: number; skipped: number; error: number };
  evals: EvalEntry[];
};

// 安全类门禁 key（前端高亮成「安全红线」组）。
const SAFETY_KEYS = new Set([
  "crisis",
  "crisis-response",
  "crisis-resource",
  "crisis-overview",
  "proactive-guard",
]);

export default function TrustMetricsPage({ initial }: { initial?: IndexData | null }) {
  const { lang, toggle } = useLang();
  // initial 由 server 组件在**构建期**读入 public/metrics/index.json 注入 → 数字直接进
  // 静态 HTML（SEO 可爬 + 无加载闪烁）；挂载后再 fetch 刷新（数据重生成后免整站重构也能更新）。
  const [data, setData] = useState<IndexData | null>(initial ?? null);
  const [err, setErr] = useState(false);
  const home = lang === "zh" ? "/" : "/en";

  useEffect(() => {
    fetch("/metrics/index.json", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((d: IndexData) => setData(d))
      .catch(() => {
        if (!initial) setErr(true); // 有构建期数据兜底时，刷新失败不报错，保留已展示的数字
      });
  }, [initial]);

  const passed = (data?.evals ?? []).filter((e) => e.status === "pass" && e.headline);
  const safety = passed.filter((e) => SAFETY_KEYS.has(e.key));
  const quality = passed.filter((e) => !SAFETY_KEYS.has(e.key));

  const genDate = data?.generated_at ? data.generated_at.slice(0, 10) : "";

  return (
    <main className="relative min-h-screen">
      {/* Nav */}
      <header className="fixed inset-x-0 top-0 z-50 glass">
        <nav className="mx-auto flex max-w-6xl items-center justify-between px-5 py-3.5">
          <Link href={home} className="flex items-center gap-2">
            <BrandMark className="h-8 w-8" />
            <span className="hidden text-base font-semibold tracking-wide text-white sm:inline">
              {BRAND.company.zh} <span className="text-slate-400">{BRAND.company.en}</span>
            </span>
          </Link>
          <div className="flex items-center gap-2.5">
            <Link
              href={home}
              className="hidden items-center gap-1.5 rounded-full border border-white/15 px-3.5 py-1.5 text-xs text-slate-300 transition hover:text-white sm:inline-flex"
            >
              <Home className="h-3.5 w-3.5" />
              {lang === "zh" ? "返回首页" : "Home"}
            </Link>
            <button
              onClick={toggle}
              className="inline-flex items-center gap-1.5 rounded-full border border-white/15 px-3.5 py-1.5 text-xs text-slate-300 transition hover:text-white"
              aria-label="switch language"
            >
              <Languages className="h-3.5 w-3.5" />
              {lang === "zh" ? "EN" : "中文"}
            </button>
          </div>
        </nav>
      </header>

      {/* Hero */}
      <section className="px-5 pb-12 pt-28 md:pt-32">
        <div className="mx-auto max-w-3xl text-center">
          <Reveal>
            <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-400/30 bg-emerald-400/10 px-3.5 py-1 text-xs font-medium text-emerald-300">
              <ShieldCheck className="h-3.5 w-3.5" />
              {lang === "zh" ? "可复现的质量门禁" : "Reproducible quality gates"}
            </span>
          </Reveal>
          <Reveal delay={0.05}>
            <h1 className="mt-5 text-4xl font-bold leading-tight text-white md:text-5xl">
              {lang === "zh" ? "不是话术，" : "Not claims —"}{" "}
              <span className="text-gradient">
                {lang === "zh" ? "是能跑出来的数字" : "numbers you can run"}
              </span>
            </h1>
          </Reveal>
          <Reveal delay={0.1}>
            <p className="mx-auto mt-5 max-w-2xl text-base text-slate-400 md:text-lg">
              {lang === "zh"
                ? "下面每一条都是代码仓库里常驻的自动化门禁（run_eval），每次改动都要跑过才能合并。数字直接来自评测产物，附样本数与复现命令——你可以自己跑一遍。"
                : "Every metric below is a permanent automated gate (run_eval) in our repo — code can't merge unless it passes. Figures come straight from the eval artifacts, with sample counts and the exact command to reproduce."}
            </p>
          </Reveal>
        </div>
      </section>

      {/* Loading / error */}
      {!data && !err && (
        <p className="pb-20 text-center text-sm text-slate-500">
          {lang === "zh" ? "加载指标中…" : "Loading metrics…"}
        </p>
      )}
      {err && (
        <p className="pb-20 text-center text-sm text-slate-500">
          {lang === "zh"
            ? "指标暂时不可用，请稍后再试或联系我们。"
            : "Metrics are temporarily unavailable — please retry later or contact us."}
        </p>
      )}

      {data && (
        <>
          {/* Summary strip */}
          <section className="px-5">
            <Reveal className="mx-auto max-w-4xl">
              <div className="grid grid-cols-2 gap-4 rounded-2xl border border-white/10 bg-ink-900/50 p-6 sm:grid-cols-4">
                <div className="text-center">
                  <div className="text-gradient text-3xl font-bold">{data.counts.pass}</div>
                  <div className="mt-1 text-[11px] text-slate-400">
                    {lang === "zh" ? "通过门禁" : "Gates passing"}
                  </div>
                </div>
                <div className="text-center">
                  <div className="text-3xl font-bold text-emerald-400">{data.counts.fail}</div>
                  <div className="mt-1 text-[11px] text-slate-400">
                    {lang === "zh" ? "失败" : "Failing"}
                  </div>
                </div>
                <div className="text-center">
                  <div className="text-3xl font-bold text-white">{safety.length}</div>
                  <div className="mt-1 text-[11px] text-slate-400">
                    {lang === "zh" ? "安全红线门禁" : "Safety gates"}
                  </div>
                </div>
                <div className="text-center">
                  <div className="text-3xl font-bold text-white">{genDate}</div>
                  <div className="mt-1 text-[11px] text-slate-400">
                    {lang === "zh" ? "生成日期" : "Generated"}
                  </div>
                </div>
              </div>
            </Reveal>
          </section>

          {/* Safety gates */}
          {safety.length > 0 && (
            <MetricGroup
              title={lang === "zh" ? "安全红线（漏一个即事故）" : "Safety red lines (one miss = incident)"}
              icon={<HeartPulse className="h-5 w-5 text-rose-400" />}
              entries={safety}
              lang={lang}
              accent="rose"
            />
          )}

          {/* Quality gates */}
          {quality.length > 0 && (
            <MetricGroup
              title={lang === "zh" ? "质量门禁（陪伴真实感 + 翻译可信）" : "Quality gates (human-like + trustworthy)"}
              icon={<BadgeCheck className="h-5 w-5 text-neon-cyan" />}
              entries={quality}
              lang={lang}
              accent="cyan"
            />
          )}

          {/* Reproduce note */}
          <section className="px-5 py-14">
            <Reveal className="mx-auto max-w-3xl">
              <div className="rounded-2xl border border-white/10 bg-ink-900/50 p-6">
                <div className="flex items-center gap-2 text-white">
                  <Terminal className="h-5 w-5 text-neon-cyan" />
                  <span className="font-semibold">{lang === "zh" ? "自己复现" : "Reproduce it yourself"}</span>
                </div>
                <p className="mt-3 text-sm leading-relaxed text-slate-400">
                  {lang === "zh"
                    ? "私有化交付后，在你自己的部署里一条命令跑出上面全部指标（缺外部资源的项会如实标记跳过，绝不伪造）："
                    : "On your own on-prem deployment, one command reproduces every metric above (items missing external resources are honestly marked skipped, never faked):"}
                </p>
                <pre className="mt-4 overflow-x-auto rounded-xl border border-white/10 bg-black/40 p-4 text-xs text-emerald-300">
                  python scripts/gen-trust-metrics.py
                </pre>
                <p className="mt-3 text-[11px] leading-relaxed text-slate-500">
                  {lang === "zh"
                    ? "指标为纯函数确定性门禁的真实结果（人设/情绪/危机安全/记忆/语音/意图）；翻译回译质量、FAQ 自解决率、向量记忆召回等依赖外部引擎/知识库/嵌入的项按环境择机纳入。"
                    : "Figures are real results of deterministic pure-function gates (persona / emotion / crisis safety / memory / voice / intent); resource-dependent items (back-translation, FAQ resolution, vector recall) are included when the environment provides the engine/KB/embeddings."}
                </p>
              </div>
            </Reveal>
          </section>
        </>
      )}

      {/* Final CTA */}
      <section className="px-5 pb-20">
        <Reveal className="mx-auto max-w-3xl">
          <div className="relative overflow-hidden rounded-3xl border border-neon-cyan/30 bg-gradient-to-br from-neon-cyan/[0.08] to-neon-violet/[0.08] p-8 text-center md:p-12">
            <h2 className="text-2xl font-bold text-white md:text-3xl">
              {lang === "zh" ? "想看它在你的场景里跑？" : "Want to see it run on your data?"}
            </h2>
            <p className="mx-auto mt-3 max-w-xl text-sm leading-relaxed text-slate-300">
              {lang === "zh"
                ? "预约一次真机演示，我们当场跑门禁、也跑你给的真实对话。"
                : "Book a live demo — we'll run the gates on the spot, and on your real conversations too."}
            </p>
            <div className="mt-7 flex flex-col items-center justify-center gap-3 sm:flex-row">
              <a
                href={CONTACT_URL}
                target="_blank"
                rel="noreferrer"
                onClick={() => track("cta_click", { where: "proof_metrics_final" })}
                className="group inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-7 py-3 font-semibold text-ink-950 transition hover:opacity-90"
              >
                <Send className="h-4 w-4" />
                {lang === "zh" ? "预约真机演示" : "Book a live demo"}
              </a>
              <Link
                href={lang === "zh" ? "/download" : "/en/download"}
                className="inline-flex items-center gap-2 rounded-full border border-white/15 px-7 py-3 font-medium text-slate-200 transition hover:border-neon-cyan/50 hover:text-white"
              >
                {lang === "zh" ? "下载桌面端" : "Download desktop"}
              </Link>
            </div>
          </div>
        </Reveal>
      </section>

      <Footer />
    </main>
  );
}

function MetricGroup({
  title,
  icon,
  entries,
  lang,
  accent,
}: {
  title: string;
  icon: React.ReactNode;
  entries: EvalEntry[];
  lang: "zh" | "en";
  accent: "rose" | "cyan";
}) {
  const ring = accent === "rose" ? "border-rose-400/20" : "border-white/10";
  return (
    <section className="px-5 py-8">
      <div className="mx-auto max-w-5xl">
        <Reveal className="mb-6 flex items-center gap-2">
          {icon}
          <h2 className="text-xl font-bold text-white md:text-2xl">{title}</h2>
        </Reveal>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {entries.map((e, i) => (
            <Reveal key={e.key} delay={i * 0.04}>
              <div className={`flex h-full flex-col rounded-2xl border ${ring} bg-ink-900/50 p-5`}>
                <div className="flex items-baseline justify-between gap-2">
                  <span className="text-gradient text-3xl font-bold">{e.headline!.value}</span>
                  <Check className="h-4 w-4 shrink-0 text-emerald-400" />
                </div>
                <h3 className="mt-2 text-sm font-semibold leading-snug text-white">
                  {e.headline!.metric[lang]}
                </h3>
                <p className="mt-1 flex-1 text-xs leading-relaxed text-slate-400">{e.label[lang]}</p>
                {typeof e.headline!.sample === "number" && (
                  <p className="mt-3 text-[11px] text-slate-500">
                    {lang === "zh" ? `样本 ${e.headline!.sample}` : `${e.headline!.sample} samples`}
                  </p>
                )}
              </div>
            </Reveal>
          ))}
        </div>
      </div>
    </section>
  );
}
