"use client";

import { Check, Minus } from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";

const WEAK = new Set(["无", "None", "—", "不支持", "Not supported"]);

export default function Compare() {
  const { t } = useLang();
  const c = t.compare;

  return (
    <section className="relative py-20">
      <div className="pointer-events-none absolute left-1/2 top-10 -z-10 h-72 w-[680px] -translate-x-1/2 rounded-full bg-neon-violet/10 blur-[120px]" />
      <div className="mx-auto max-w-5xl px-5">
        <Reveal className="mb-10 text-center">
          <span className="inline-flex items-center gap-1.5 rounded-full border border-neon-cyan/30 bg-neon-cyan/10 px-3 py-1 text-xs font-medium text-neon-cyan">
            {c.badge}
          </span>
          <h2 className="mt-4 text-3xl font-bold text-white md:text-4xl">{c.title}</h2>
          <p className="mx-auto mt-3 max-w-2xl text-slate-400">{c.subtitle}</p>
        </Reveal>

        <Reveal>
          {/* Mobile: stacked cards (no horizontal scroll) */}
          <div className="space-y-3 md:hidden">
            {c.rows.map((row) => (
              <div key={row.label} className="rounded-2xl border border-white/10 bg-ink-900/60 p-4">
                <div className="text-sm font-semibold text-white">{row.label}</div>
                <div className="mt-3 space-y-2 text-sm">
                  <div className="flex items-start justify-between gap-3 rounded-lg bg-neon-cyan/[0.06] px-3 py-2">
                    <span className="text-xs font-medium text-neon-cyan">{c.cols[0]}</span>
                    <span className="inline-flex items-center gap-1.5 text-right font-medium text-white">
                      <Check className="h-4 w-4 shrink-0 text-emerald-400" />
                      {row.us}
                    </span>
                  </div>
                  <div className="flex items-start justify-between gap-3 px-3">
                    <span className="text-xs text-slate-500">{c.cols[1]}</span>
                    <span className="inline-flex items-center gap-1.5 text-right text-slate-400">
                      {WEAK.has(row.them) && <Minus className="h-3.5 w-3.5 shrink-0 text-red-400/70" />}
                      {row.them}
                    </span>
                  </div>
                  <div className="flex items-start justify-between gap-3 px-3">
                    <span className="text-xs text-slate-500">{c.cols[2]}</span>
                    <span className="inline-flex items-center gap-1.5 text-right text-slate-400">
                      {WEAK.has(row.manual) && <Minus className="h-3.5 w-3.5 shrink-0 text-red-400/70" />}
                      {row.manual}
                    </span>
                  </div>
                </div>
              </div>
            ))}
          </div>

          {/* Desktop / tablet: full comparison table */}
          <div className="hidden md:block">
            <div className="overflow-hidden rounded-2xl border border-white/10 bg-ink-900/60">
              {/* header */}
              <div className="grid grid-cols-[1.1fr_1.2fr_1fr_1fr] border-b border-white/10 bg-ink-800/50">
                <span className="px-4 py-4" />
                <span className="relative px-4 py-4 text-center">
                  <span className="absolute inset-x-2 inset-y-1 -z-0 rounded-xl bg-gradient-to-b from-neon-cyan/15 to-transparent" />
                  <span className="relative font-bold text-neon-cyan">{c.cols[0]}</span>
                </span>
                <span className="px-4 py-4 text-center font-medium text-slate-400">{c.cols[1]}</span>
                <span className="px-4 py-4 text-center font-medium text-slate-400">{c.cols[2]}</span>
              </div>

              {/* rows */}
              {c.rows.map((row, ri) => (
                <div
                  key={row.label}
                  className={`grid grid-cols-[1.1fr_1.2fr_1fr_1fr] items-center text-sm ${
                    ri % 2 ? "bg-white/[0.02]" : ""
                  }`}
                >
                  <span className="px-4 py-3.5 font-medium text-slate-300">{row.label}</span>
                  <span className="relative px-4 py-3.5 text-center">
                    <span className="absolute inset-x-2 inset-y-0 -z-0 bg-neon-cyan/[0.05]" />
                    <span className="relative inline-flex items-center gap-1.5 font-medium text-white">
                      <Check className="h-4 w-4 shrink-0 text-emerald-400" />
                      {row.us}
                    </span>
                  </span>
                  <span className="px-4 py-3.5 text-center text-slate-500">
                    {WEAK.has(row.them) ? (
                      <span className="inline-flex items-center gap-1.5">
                        <Minus className="h-3.5 w-3.5 text-red-400/70" />
                        {row.them}
                      </span>
                    ) : (
                      row.them
                    )}
                  </span>
                  <span className="px-4 py-3.5 text-center text-slate-500">
                    {WEAK.has(row.manual) ? (
                      <span className="inline-flex items-center gap-1.5">
                        <Minus className="h-3.5 w-3.5 text-red-400/70" />
                        {row.manual}
                      </span>
                    ) : (
                      row.manual
                    )}
                  </span>
                </div>
              ))}
            </div>
          </div>
        </Reveal>
      </div>
    </section>
  );
}
