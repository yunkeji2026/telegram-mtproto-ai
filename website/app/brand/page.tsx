"use client";

import { useLang } from "@/components/LanguageContext";
import { BRAND, PRODUCT_ORDER } from "@/lib/brand";
import { PRODUCT_ICONS } from "@/components/productMeta";
import BrandMark from "@/components/BrandMark";
import { CONTACT_URL } from "@/lib/site";
import { track } from "@/lib/track";
import { ArrowRight, ShieldCheck } from "lucide-react";

const COPY = {
  zh: {
    kicker: "品牌故事",
    heroTitle: "让沟通，无界",
    heroDesc: "无界科技 BOUNDLESS —— 用 AI 让任何人，以任意面孔、声音、语言，实时沟通并自动成交。",
    storyHead: "沟通，本不该有边界",
    storyParas: [
      "但现实里，边界无处不在——",
      "一张脸，限制了你能成为谁；一种声音，困住了你能扮演谁；一门语言，隔开了你与世界；一道平台的围墙，挡住了客户走向你。",
      "无界，为打破这一切而生。",
    ],
    wallsHead: "我们用 AI 拆掉五道墙",
    closingHead: "底座本身，也没有边界",
    closing:
      "私有部署、数据不出网、无审查限制——你想怎么用，就怎么用。这才是「无界」二字真正的底气。",
    slogan: "无界。让沟通，真正没有边界。",
    breakLabel: "打破",
    productsHead: "五条产品线 · 打破五界",
    ctaTitle: "把「无界」用起来",
    ctaDesc: "一句话告诉我们你的场景，我们给方案与报价。",
    ctaBtn: "联系我们",
    backHome: "返回首页",
    engineName: "无界底座 BOUNDLESS Engine",
  },
  en: {
    kicker: "Brand Story",
    heroTitle: "Communication, Boundless.",
    heroDesc:
      "BOUNDLESS — let anyone communicate and close deals in real time, with any face, any voice, any language.",
    storyHead: "Communication should have no borders",
    storyParas: [
      "Yet in reality, borders are everywhere —",
      "A face limits who you can be; a voice limits who you can play; a language separates you from the world; a platform's walls keep customers from reaching you.",
      "BOUNDLESS was born to break them all.",
    ],
    wallsHead: "We tear down five walls with AI",
    closingHead: "Even the foundation is borderless",
    closing:
      "Private deployment, data stays off-net, no censorship limits — use it however you want. That is what truly backs the name BOUNDLESS.",
    slogan: "BOUNDLESS. Communication, with no borders at all.",
    breakLabel: "Breaks",
    productsHead: "Five product lines · Five barriers broken",
    ctaTitle: "Put BOUNDLESS to work",
    ctaDesc: "Tell us your scenario in one line — we'll send a plan and a quote.",
    ctaBtn: "Contact us",
    backHome: "Back to home",
    engineName: "BOUNDLESS Engine",
  },
};

export default function BrandPage() {
  const { lang } = useLang();
  const c = COPY[lang];

  return (
    <main className="relative min-h-screen overflow-hidden bg-ink-950 text-white">
      {/* 背景光晕 */}
      <div className="pointer-events-none absolute inset-0">
        <div className="absolute -top-40 left-1/2 h-[480px] w-[480px] -translate-x-1/2 rounded-full bg-neon-violet/20 blur-[140px]" />
        <div className="absolute top-1/3 -left-40 h-[360px] w-[360px] rounded-full bg-neon-cyan/15 blur-[120px]" />
      </div>

      <div className="relative mx-auto max-w-5xl px-5 pb-24 pt-28">
        {/* Hero */}
        <section className="text-center">
          <div className="mx-auto mb-6 flex items-center justify-center gap-3">
            <BrandMark className="h-14 w-14" />
            <span className="text-2xl font-bold tracking-wide">
              {BRAND.company.zh} <span className="text-slate-400">{BRAND.company.en}</span>
            </span>
          </div>
          <p className="mb-3 text-xs font-medium uppercase tracking-[0.3em] text-neon-cyan">
            {c.kicker}
          </p>
          <h1 className="bg-gradient-to-r from-neon-cyan via-white to-neon-violet bg-clip-text text-4xl font-black leading-tight text-transparent sm:text-6xl">
            {c.heroTitle}
          </h1>
          <p className="mx-auto mt-6 max-w-2xl text-base leading-relaxed text-slate-300 sm:text-lg">
            {c.heroDesc}
          </p>
        </section>

        {/* 品牌故事 */}
        <section className="mx-auto mt-20 max-w-3xl">
          <h2 className="text-center text-2xl font-bold sm:text-3xl">{c.storyHead}</h2>
          <div className="mt-6 space-y-4 text-center text-base leading-relaxed text-slate-300">
            {c.storyParas.map((p, i) => (
              <p key={i} className={i === c.storyParas.length - 1 ? "text-lg font-semibold text-white" : ""}>
                {p}
              </p>
            ))}
          </div>
        </section>

        {/* 破五界 · 产品矩阵 */}
        <section className="mt-16">
          <h2 className="mb-2 text-center text-sm font-medium uppercase tracking-[0.25em] text-slate-400">
            {c.wallsHead}
          </h2>
          <h3 className="mb-8 text-center text-2xl font-bold sm:text-3xl">{c.productsHead}</h3>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {PRODUCT_ORDER.map((key, idx) => {
              const p = BRAND.products[key];
              const Icon = PRODUCT_ICONS[key];
              return (
                <div
                  key={key}
                  className="group relative overflow-hidden rounded-2xl border border-white/10 bg-white/[0.03] p-5 transition hover:border-neon-cyan/40 hover:bg-white/[0.05]"
                >
                  <div className="mb-4 flex items-center justify-between">
                    <span className="flex h-11 w-11 items-center justify-center rounded-xl bg-gradient-to-br from-neon-cyan/20 to-neon-violet/20 text-neon-cyan">
                      <Icon className="h-5 w-5" />
                    </span>
                    <span className="text-xs font-mono text-slate-600">0{idx + 1}</span>
                  </div>
                  <div className="flex items-baseline gap-2">
                    <span className="text-xl font-bold text-white">{p.zh}</span>
                    <span className="text-sm font-semibold text-neon-cyan">{p.en}</span>
                  </div>
                  <p className="mt-0.5 text-[11px] text-slate-500">{p.alt}</p>
                  <p className="mt-3 text-sm leading-relaxed text-slate-300">{p.desc[lang]}</p>
                  <p className="mt-3 inline-flex items-center gap-1 rounded-full bg-neon-violet/10 px-2.5 py-1 text-[11px] font-medium text-neon-violet">
                    {c.breakLabel} · {p.break[lang]}
                  </p>
                </div>
              );
            })}

            {/* 底座卡片 */}
            <div className="relative overflow-hidden rounded-2xl border border-neon-cyan/30 bg-gradient-to-br from-neon-cyan/[0.08] to-neon-violet/[0.08] p-5">
              <div className="mb-4 flex h-11 w-11 items-center justify-center rounded-xl bg-neon-cyan/20 text-neon-cyan">
                <ShieldCheck className="h-5 w-5" />
              </div>
              <div className="text-xl font-bold text-white">{c.engineName}</div>
              <p className="mt-3 text-sm leading-relaxed text-slate-300">{c.closing}</p>
            </div>
          </div>
        </section>

        {/* 收尾 slogan */}
        <section className="mx-auto mt-20 max-w-3xl text-center">
          <h2 className="text-lg font-semibold text-slate-300">{c.closingHead}</h2>
          <p className="mt-4 bg-gradient-to-r from-neon-cyan to-neon-violet bg-clip-text text-2xl font-black text-transparent sm:text-3xl">
            {c.slogan}
          </p>
        </section>

        {/* CTA */}
        <section className="mx-auto mt-16 max-w-xl rounded-3xl border border-white/10 bg-white/[0.03] p-8 text-center">
          <h3 className="text-xl font-bold">{c.ctaTitle}</h3>
          <p className="mt-2 text-sm text-slate-400">{c.ctaDesc}</p>
          <div className="mt-6 flex flex-wrap items-center justify-center gap-3">
            <a
              href={CONTACT_URL}
              target="_blank"
              rel="noreferrer"
              onClick={() => track("cta_click", { where: "brand_page" })}
              className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-6 py-3 text-sm font-semibold text-ink-950 transition hover:opacity-90"
            >
              {c.ctaBtn}
              <ArrowRight className="h-4 w-4" />
            </a>
            <a
              href="/"
              className="inline-flex items-center gap-2 rounded-full border border-white/15 px-6 py-3 text-sm text-slate-200 transition hover:border-white/30"
            >
              {c.backHome}
            </a>
          </div>
        </section>
      </div>
    </main>
  );
}
