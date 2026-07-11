"use client";

import { Quote, Star } from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import CountUp from "./fx/CountUp";
import { BrandGlyph, BRAND_BG } from "./brandIcons";

export default function TrustBar() {
  const { t } = useLang();

  return (
    <section className="relative border-y border-white/5 py-16">
      <div className="mx-auto max-w-7xl px-5">
        {/* platform logo wall */}
        <p className="text-center text-xs uppercase tracking-widest text-slate-500">
          {t.trust.platformsLabel}
        </p>
        <div className="mt-6 flex flex-wrap items-center justify-center gap-3 md:gap-4">
          {t.trust.platforms.map((p) => (
            <div
              key={p}
              className="group flex items-center gap-2.5 rounded-xl border border-white/10 bg-white/[0.03] px-4 py-2.5 transition hover:border-white/20 hover:bg-white/[0.06]"
            >
              <span
                className="grid h-8 w-8 place-items-center rounded-lg text-white transition group-hover:scale-110"
                style={{ background: BRAND_BG[p] ?? "#64748b" }}
              >
                <BrandGlyph name={p} className="h-[18px] w-[18px]" />
              </span>
              <span className="text-sm font-medium text-slate-300 transition group-hover:text-white">
                {p}
              </span>
            </div>
          ))}
        </div>

        {/* stats */}
        <div className="mt-14 text-center">
          <h2 className="text-2xl font-bold text-white md:text-3xl">{t.trust.statsTitle}</h2>
        </div>
        <div className="mt-8 grid grid-cols-2 gap-4 md:grid-cols-4">
          {t.trust.stats.map((s, i) => (
            <Reveal key={s.label} delay={i * 0.06}>
              <div className="glass card-hover rounded-2xl px-4 py-6 text-center">
                <div className="text-gradient text-3xl font-bold md:text-4xl">
                  <CountUp value={s.value} suffix={s.suffix} />
                </div>
                <div className="mt-2 text-xs text-slate-400">{s.label}</div>
              </div>
            </Reveal>
          ))}
        </div>

        {/* testimonials */}
        <div className="mt-16 text-center">
          <h2 className="text-2xl font-bold text-white md:text-3xl">{t.trust.testimonialsTitle}</h2>
        </div>
        <div className="mt-8 grid gap-5 md:grid-cols-3">
          {t.trust.testimonials.map((tm, i) => (
            <Reveal key={tm.name} delay={i * 0.08}>
              <figure className="card-hover flex h-full flex-col rounded-2xl border border-white/10 bg-ink-900/60 p-6">
                <div className="flex items-center justify-between">
                  <Quote className="h-6 w-6 text-neon-violet/70" />
                  <span className="flex gap-0.5">
                    {[0, 1, 2, 3, 4].map((s) => (
                      <Star key={s} className="h-3.5 w-3.5 fill-amber-400 text-amber-400" />
                    ))}
                  </span>
                </div>
                <blockquote className="mt-3 flex-1 text-sm leading-relaxed text-slate-300">
                  &ldquo;{tm.quote}&rdquo;
                </blockquote>
                <figcaption className="mt-4 flex items-center gap-3">
                  <span className="grid h-9 w-9 place-items-center rounded-full bg-gradient-to-br from-neon-cyan/30 to-neon-violet/30 text-sm font-semibold text-white ring-1 ring-white/10">
                    {tm.name.slice(0, 1)}
                  </span>
                  <span>
                    <span className="block text-sm font-medium text-white">{tm.name}</span>
                    <span className="block text-xs text-slate-500">{tm.role}</span>
                  </span>
                </figcaption>
              </figure>
            </Reveal>
          ))}
        </div>

        <p className="mx-auto mt-8 max-w-3xl text-center text-[11px] leading-relaxed text-slate-500">
          {t.trust.disclaimer}
        </p>
      </div>
    </section>
  );
}
