"use client";

import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import { BRAND, PRODUCT_ORDER } from "@/lib/brand";
import { PRODUCT_IMG, PRODUCT_ANCHOR, PRODUCT_LANDING } from "./productMeta";
import { track } from "@/lib/track";
import { ArrowRight, ShieldCheck } from "lucide-react";

const COPY = {
  zh: {
    kicker: "产品矩阵",
    head: "一个无界底座，五条产品线",
    sub: "打破容貌、声音、身份、语言、成交五道边界——每条产品线都可单独选用，也能组合成完整的「数字分身 + 自动成交」闭环。",
    breakLabel: "打破",
    engineName: "无界底座 BOUNDLESS Engine",
    engineDesc: "五条产品线共享同一私有化底座：数据不出网、无审查限制、全程 USDT 结算。",
    ctaPrimary: "查看套餐与价格",
    ctaSecondary: "了解品牌故事",
  },
  en: {
    kicker: "Product Matrix",
    head: "One boundless core, five product lines",
    sub: "Break the barriers of face, voice, identity, language and sales — pick any line on its own, or combine them into a full \"digital twin + auto-closing\" loop.",
    breakLabel: "Breaks",
    engineName: "BOUNDLESS Engine",
    engineDesc: "All five lines share one private-deployment core: data stays off-net, no censorship limits, settled in USDT.",
    ctaPrimary: "View plans & pricing",
    ctaSecondary: "Read the brand story",
  },
} as const;

export default function ProductMatrix() {
  const { lang } = useLang();
  const c = COPY[lang];

  return (
    <section id="products" className="relative py-24">
      <div className="mx-auto max-w-7xl px-5">
        <Reveal>
          <p className="text-center text-xs font-medium uppercase tracking-[0.28em] text-neon-cyan">
            {c.kicker}
          </p>
          <h2 className="mx-auto mt-3 max-w-3xl text-center text-3xl font-bold text-white md:text-4xl">
            {c.head}
          </h2>
          <p className="mx-auto mt-4 max-w-2xl text-center text-base text-slate-400">
            {c.sub}
          </p>
        </Reveal>

        <div className="mt-12 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {PRODUCT_ORDER.map((key, idx) => {
            const p = BRAND.products[key];
            // 有独立落地页的产品线跳落地页（更完整的卖点+真实样片），其余回退首页锚点
            const landing = PRODUCT_LANDING[key];
            const href = landing ? (lang === "zh" ? landing : `/en${landing}`) : PRODUCT_ANCHOR[key];
            return (
              <Reveal key={key} delay={idx * 0.05}>
                <a
                  href={href}
                  onClick={() => track("product_click", { key, where: "matrix" })}
                  className="group relative flex h-full flex-col overflow-hidden rounded-2xl border border-white/10 bg-white/[0.03] p-5 transition hover:border-neon-cyan/40 hover:bg-white/[0.05]"
                >
                  <div className="mb-4 flex items-center justify-between">
                    <img
                      src={PRODUCT_IMG[key]}
                      alt={`${p.zh} ${p.en}`}
                      width={48}
                      height={48}
                      className="h-12 w-12 object-contain transition-transform group-hover:scale-110"
                      draggable={false}
                    />
                    <span className="font-mono text-xs text-slate-600">0{idx + 1}</span>
                  </div>
                  <div className="flex items-baseline gap-2">
                    <span className="text-xl font-bold text-white">{p.zh}</span>
                    <span className="text-sm font-semibold text-neon-cyan">{p.en}</span>
                  </div>
                  <p className="mt-0.5 text-[11px] text-slate-500">{p.alt}</p>
                  <p className="mt-3 flex-1 text-sm leading-relaxed text-slate-300">{p.desc[lang]}</p>
                  <p className="mt-3 inline-flex w-fit items-center gap-1 rounded-full bg-neon-violet/10 px-2.5 py-1 text-[11px] font-medium text-neon-violet">
                    {c.breakLabel} · {p.break[lang]}
                  </p>
                </a>
              </Reveal>
            );
          })}

          {/* 无界底座卡片 */}
          <Reveal delay={PRODUCT_ORDER.length * 0.05}>
            <div className="relative flex h-full flex-col overflow-hidden rounded-2xl border border-neon-cyan/30 bg-gradient-to-br from-neon-cyan/[0.08] to-neon-violet/[0.08] p-5">
              <span className="mb-4 flex h-11 w-11 items-center justify-center rounded-xl bg-neon-cyan/20 text-neon-cyan">
                <ShieldCheck className="h-5 w-5" />
              </span>
              <div className="text-xl font-bold text-white">{c.engineName}</div>
              <p className="mt-3 flex-1 text-sm leading-relaxed text-slate-300">{c.engineDesc}</p>
            </div>
          </Reveal>
        </div>

        <div className="mt-10 flex flex-wrap items-center justify-center gap-3">
          <a
            href="#pricing"
            onClick={() => track("cta_click", { where: "matrix_primary" })}
            className="group inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-6 py-3 text-sm font-semibold text-ink-950 transition hover:opacity-90"
          >
            {c.ctaPrimary}
            <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-1" />
          </a>
          <a
            href="/brand"
            onClick={() => track("cta_click", { where: "matrix_brand" })}
            className="inline-flex items-center gap-2 rounded-full border border-white/15 px-6 py-3 text-sm text-slate-200 transition hover:border-neon-cyan/50 hover:text-white"
          >
            {c.ctaSecondary}
          </a>
        </div>
      </div>
    </section>
  );
}
