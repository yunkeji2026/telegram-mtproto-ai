"use client";

import Link from "next/link";
import {
  ArrowRight,
  ArrowDown,
  Check,
  Send,
  Languages,
  ShieldCheck,
  AudioLines,
  PlayCircle,
  Home,
} from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import BeforeAfter from "./fx/BeforeAfter";
import { AudioClip, VideoClip } from "./fx/MediaClips";
import BrandMark from "./BrandMark";
import Footer from "./Footer";
import { LANDINGS, LANDING_MEDIA, type LandingKey } from "@/lib/landingContent";
import { BRAND } from "@/lib/brand";
import { CONTACT_URL } from "@/lib/site";
import { track } from "@/lib/track";

/** 产品线落地页骨架：一页一卖点，真实媒体证据前置，CTA 直达 Telegram。
 *  全局 Chrome（AIChat 悬浮球 / Cookie / 埋点）由 layout 提供，这里不重复。 */

function LandingNav({ product }: { product: LandingKey }) {
  const { lang, toggle } = useLang();
  const home = lang === "zh" ? "/" : "/en";
  return (
    <header className="fixed inset-x-0 top-0 z-50 glass">
      <nav className="mx-auto flex max-w-6xl items-center justify-between px-5 py-3.5">
        <div className="flex items-center gap-3">
          <Link href={home} className="flex items-center gap-2">
            <BrandMark className="h-8 w-8" />
            <span className="hidden text-base font-semibold tracking-wide text-white sm:inline">
              {BRAND.company.zh} <span className="text-slate-400">{BRAND.company.en}</span>
            </span>
          </Link>
          <span className="hidden rounded-full border border-white/10 bg-white/5 px-2.5 py-0.5 text-[11px] text-slate-300 md:inline">
            {LANDINGS[product].productLine[lang]}
          </span>
        </div>
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
          <a
            href={CONTACT_URL}
            target="_blank"
            rel="noreferrer"
            onClick={() => track("cta_click", { where: `landing_${product}_nav` })}
            className="inline-flex items-center gap-1.5 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-4 py-1.5 text-xs font-semibold text-ink-950 transition hover:opacity-90"
          >
            <Send className="h-3.5 w-3.5" />
            {lang === "zh" ? "在线咨询" : "Chat now"}
          </a>
        </div>
      </nav>
    </header>
  );
}

function DemoBlock({ product }: { product: LandingKey }) {
  const { lang } = useLang();
  const d = LANDINGS[product].demo;

  return (
    <div className="mx-auto mt-14 max-w-3xl">
      <Reveal className="text-center">
        <h2 className="text-2xl font-bold text-white md:text-3xl">{d.title[lang]}</h2>
        <p className="mx-auto mt-2 max-w-xl text-sm text-slate-400">{d.subtitle[lang]}</p>
      </Reveal>

      <Reveal delay={0.08} className="mt-7">
        {product === "voice" && (
          <div className="grid gap-5 md:grid-cols-[1fr_auto]">
            <div className="space-y-2.5">
              {LANDING_MEDIA.voiceClips.map((clip) => (
                <AudioClip key={clip.src} label={clip.label[lang]} src={clip.src} />
              ))}
              <div className="flex items-center gap-2 pt-1 text-[11px] text-slate-500">
                <ShieldCheck className="h-3.5 w-3.5 shrink-0 text-emerald-400/80" />
                {d.realNote[lang]}
              </div>
            </div>
            <VideoClip
              src={LANDING_MEDIA.dhVideoEn.src}
              poster={LANDING_MEDIA.dhVideoEn.poster}
              pending={d.realNote[lang]}
            />
          </div>
        )}

        {product === "face" && (
          <div className="grid items-start gap-5 md:grid-cols-[1.25fr_auto]">
            <div>
              <BeforeAfter
                before={LANDING_MEDIA.faceSwap.before}
                after={LANDING_MEDIA.faceSwap.after}
                beforeLabel={lang === "zh" ? "原始" : "Original"}
                afterLabel={lang === "zh" ? "换脸后" : "Swapped"}
                hint={lang === "zh" ? "拖动查看前后" : "Drag to compare"}
              />
              <div className="mt-3 flex items-center gap-2 text-[11px] text-slate-500">
                <ShieldCheck className="h-3.5 w-3.5 shrink-0 text-emerald-400/80" />
                {d.realNote[lang]}
              </div>
            </div>
            <VideoClip
              src={LANDING_MEDIA.dhVideoZh.src}
              poster={LANDING_MEDIA.dhVideoZh.poster}
              pending={d.realNote[lang]}
            />
          </div>
        )}

        {product === "interpreting" && (
          <div className="mx-auto max-w-xl space-y-3">
            <AudioClip
              label={LANDING_MEDIA.interpPair.src.label[lang]}
              src={LANDING_MEDIA.interpPair.src.file}
            />
            <div className="flex items-center justify-center gap-2 text-xs text-slate-400">
              <ArrowDown className="h-4 w-4 text-neon-cyan" />
              {lang === "zh" ? "引擎实时同传（保留同一音色）" : "Engine interprets live (same voice)"}
            </div>
            <AudioClip
              label={LANDING_MEDIA.interpPair.out.label[lang]}
              src={LANDING_MEDIA.interpPair.out.file}
            />
            <div className="flex items-center gap-2 pt-1 text-[11px] text-slate-500">
              <ShieldCheck className="h-3.5 w-3.5 shrink-0 text-emerald-400/80" />
              {d.realNote[lang]}
            </div>
          </div>
        )}
      </Reveal>
    </div>
  );
}

export default function ProductLanding({ product }: { product: LandingKey }) {
  const { lang } = useLang();
  const L = LANDINGS[product];

  return (
    <main className="relative min-h-screen">
      <LandingNav product={product} />

      {/* Hero */}
      <section className="relative overflow-hidden px-5 pb-16 pt-28 md:pt-32">
        <div className="mx-auto max-w-3xl text-center">
          <Reveal>
            <span className="inline-flex items-center gap-1.5 rounded-full border border-neon-cyan/30 bg-neon-cyan/10 px-3.5 py-1 text-xs font-medium text-neon-cyan">
              {product === "voice" ? <AudioLines className="h-3.5 w-3.5" /> : product === "face" ? <PlayCircle className="h-3.5 w-3.5" /> : <Languages className="h-3.5 w-3.5" />}
              {L.productLine[lang]}
            </span>
          </Reveal>
          <Reveal delay={0.05}>
            <h1 className="mt-5 text-4xl font-bold leading-tight text-white md:text-5xl">
              <span className="whitespace-nowrap">{L.hero.title[lang]}</span>{" "}
              <span className="text-gradient whitespace-nowrap">{L.hero.accent[lang]}</span>
            </h1>
          </Reveal>
          <Reveal delay={0.1}>
            <p className="mx-auto mt-5 max-w-2xl text-base text-slate-400 md:text-lg">{L.hero.subtitle[lang]}</p>
          </Reveal>
          <Reveal delay={0.15}>
            <ul className="mx-auto mt-6 flex max-w-2xl flex-wrap items-center justify-center gap-x-6 gap-y-2">
              {L.hero.points.map((pt) => (
                <li key={pt.en} className="flex items-center gap-1.5 text-xs text-slate-300">
                  <Check className="h-3.5 w-3.5 shrink-0 text-emerald-400" />
                  {pt[lang]}
                </li>
              ))}
            </ul>
          </Reveal>
          <Reveal delay={0.2}>
            <div className="mt-8 flex flex-col items-center justify-center gap-3 sm:flex-row">
              <a
                href={CONTACT_URL}
                target="_blank"
                rel="noreferrer"
                onClick={() => track("cta_click", { where: `landing_${product}_hero` })}
                className="group inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-7 py-3 font-medium text-ink-950 transition hover:opacity-90"
              >
                {lang === "zh" ? "预约真机演示" : "Book a live demo"}
                <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-1" />
              </a>
              <a
                href="#demo"
                className="inline-flex items-center gap-2 rounded-full border border-white/15 px-7 py-3 font-medium text-slate-200 transition hover:border-neon-cyan/50 hover:text-white"
              >
                {lang === "zh" ? "先看真实样片" : "See real samples"}
                <ArrowDown className="h-4 w-4" />
              </a>
            </div>
          </Reveal>
          <Reveal delay={0.25}>
            <p className="mt-5 flex items-center justify-center gap-2 text-xs text-slate-500">
              <ShieldCheck className="h-3.5 w-3.5 text-emerald-400/80" />
              {lang === "zh"
                ? "本地部署 · 数据不出机房 · USDT 结算 · 产出可验真"
                : "Private deployment · data stays in-house · USDT settlement · verifiable output"}
            </p>
          </Reveal>
        </div>

        {/* Demo（真实媒体证据） */}
        <div id="demo" className="scroll-mt-24">
          <DemoBlock product={product} />
        </div>
      </section>

      {/* Capabilities */}
      <section className="border-y border-white/5 bg-white/[0.015] px-5 py-16">
        <div className="mx-auto max-w-5xl">
          <Reveal className="text-center">
            <h2 className="text-2xl font-bold text-white md:text-3xl">
              {lang === "zh" ? "硬核能力 · 每条都能当场验证" : "Hard capabilities, verifiable live"}
            </h2>
          </Reveal>
          <div className="mt-9 grid gap-4 sm:grid-cols-2">
            {L.caps.map((c, i) => (
              <Reveal key={c.title.en} delay={i * 0.05}>
                <div className="flex h-full flex-col rounded-2xl border border-white/10 bg-ink-900/50 p-5">
                  <h3 className="font-semibold text-white">{c.title[lang]}</h3>
                  <p className="mt-2 flex-1 text-sm leading-relaxed text-slate-400">{c.desc[lang]}</p>
                  <p className="mt-3 inline-flex items-center gap-1.5 text-xs font-medium text-neon-cyan">
                    <Check className="h-3.5 w-3.5" />
                    {c.proof[lang]}
                  </p>
                </div>
              </Reveal>
            ))}
          </div>
        </div>
      </section>

      {/* Steps */}
      <section className="px-5 py-16">
        <div className="mx-auto max-w-4xl">
          <Reveal className="text-center">
            <h2 className="text-2xl font-bold text-white md:text-3xl">
              {lang === "zh" ? "三步上手" : "Three steps to start"}
            </h2>
          </Reveal>
          <div className="mt-9 grid gap-4 md:grid-cols-3">
            {L.steps.map((s, i) => (
              <Reveal key={s.title.en} delay={i * 0.06}>
                <div className="relative h-full rounded-2xl border border-white/10 bg-ink-900/50 p-5 pt-6">
                  <span className="absolute -top-3.5 left-5 grid h-7 w-7 place-items-center rounded-full bg-gradient-to-br from-neon-cyan to-neon-violet text-xs font-bold text-ink-950">
                    {i + 1}
                  </span>
                  <h3 className="font-semibold text-white">{s.title[lang]}</h3>
                  <p className="mt-2 text-sm leading-relaxed text-slate-400">{s.desc[lang]}</p>
                </div>
              </Reveal>
            ))}
          </div>
        </div>
      </section>

      {/* FAQ */}
      <section className="border-y border-white/5 bg-white/[0.015] px-5 py-16">
        <div className="mx-auto max-w-3xl">
          <Reveal className="text-center">
            <h2 className="text-2xl font-bold text-white md:text-3xl">{lang === "zh" ? "常见问题" : "FAQ"}</h2>
          </Reveal>
          <div className="mt-8 space-y-3">
            {L.faq.map((f, i) => (
              <Reveal key={f.q.en} delay={i * 0.04}>
                <details className="group rounded-2xl border border-white/10 bg-ink-900/50 p-5 open:border-neon-cyan/25">
                  <summary className="cursor-pointer list-none font-medium text-white marker:hidden">
                    {f.q[lang]}
                  </summary>
                  <p className="mt-3 text-sm leading-relaxed text-slate-400">{f.a[lang]}</p>
                </details>
              </Reveal>
            ))}
          </div>
          <Reveal delay={0.1} className="mt-6 text-center">
            <Link
              href={lang === "zh" ? "/#faq" : "/en#faq"}
              className="text-sm text-slate-400 underline-offset-4 transition hover:text-neon-cyan hover:underline"
            >
              {lang === "zh" ? "更多问题 → 首页完整 FAQ" : "More questions → full FAQ on the homepage"}
            </Link>
          </Reveal>
        </div>
      </section>

      {/* Final CTA */}
      <section className="px-5 py-20">
        <Reveal className="mx-auto max-w-3xl">
          <div className="relative overflow-hidden rounded-3xl border border-neon-cyan/30 bg-gradient-to-br from-neon-cyan/[0.08] to-neon-violet/[0.08] p-8 text-center md:p-12">
            <h2 className="text-2xl font-bold text-white md:text-3xl">{L.finalCta.title[lang]}</h2>
            <p className="mx-auto mt-3 max-w-xl text-sm leading-relaxed text-slate-300">{L.finalCta.desc[lang]}</p>
            <div className="mt-7 flex flex-col items-center justify-center gap-3 sm:flex-row">
              <a
                href={CONTACT_URL}
                target="_blank"
                rel="noreferrer"
                onClick={() => track("cta_click", { where: `landing_${product}_final` })}
                className="group inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-7 py-3 font-semibold text-ink-950 transition hover:opacity-90"
              >
                <Send className="h-4 w-4" />
                {lang === "zh" ? "Telegram 一对一咨询" : "1-on-1 on Telegram"}
                <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-1" />
              </a>
              <Link
                href={lang === "zh" ? "/#pricing" : "/en#pricing"}
                className="inline-flex items-center gap-2 rounded-full border border-white/15 px-7 py-3 font-medium text-slate-200 transition hover:border-neon-cyan/50 hover:text-white"
              >
                {lang === "zh" ? "查看套餐与价格" : "Plans & pricing"}
              </Link>
            </div>
          </div>
        </Reveal>
      </section>

      <Footer />
    </main>
  );
}
