"use client";

import { useState } from "react";
import { Check, ArrowRight, ServerCog, Wrench, TrendingUp, type LucideIcon } from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import { track } from "@/lib/track";

const MODEL_ICON: Record<string, LucideIcon> = {
  service: ServerCog,
  managed: Wrench,
  invest: TrendingUp,
};

export default function EngagementModels() {
  const { t } = useLang();
  const e = t.engage;
  const rt = t.realtime;
  const [active, setActive] = useState("managed");

  return (
    <section id="engage" className="relative bg-white/[0.015] py-24">
      <div className="pointer-events-none absolute inset-0 -z-10 bg-[radial-gradient(ellipse_at_bottom,rgba(139,92,246,0.06),transparent_60%)]" />
      <div className="mx-auto max-w-6xl px-5">
        <Reveal className="mb-10 text-center">
          <span className="inline-flex items-center gap-1.5 rounded-full border border-neon-violet/30 bg-neon-violet/10 px-3 py-1 text-xs font-medium text-neon-violet">
            {e.badge}
          </span>
          <h2 className="mt-4 text-3xl font-bold text-white md:text-4xl">{e.title}</h2>
          <p className="mx-auto mt-4 max-w-3xl text-slate-400">{e.subtitle}</p>
        </Reveal>

        {/* selector */}
        <Reveal className="mb-8">
          <div className="flex flex-col items-center gap-3">
            <span className="text-sm text-slate-400">{e.selectorTitle}</span>
            <div className="flex flex-wrap justify-center gap-2">
              {e.selector.map((s) => {
                const on = active === s.id;
                return (
                  <button
                    key={s.id}
                    onClick={() => {
                      setActive(s.id);
                      track("engage_select", { id: s.id });
                    }}
                    className={`rounded-full border px-4 py-2 text-sm font-medium transition ${
                      on
                        ? "border-transparent bg-gradient-to-r from-neon-cyan to-neon-violet text-ink-950"
                        : "border-white/15 bg-white/5 text-slate-300 hover:border-neon-cyan/40"
                    }`}
                  >
                    {s.label}
                  </button>
                );
              })}
            </div>
          </div>
        </Reveal>

        {/* model cards */}
        <div className="grid gap-5 lg:grid-cols-3">
          {e.models.map((m, i) => {
            const Icon = MODEL_ICON[m.id] ?? ServerCog;
            const on = active === m.id;
            return (
              <Reveal key={m.id} delay={i * 0.06} className="h-full">
                <button
                  onClick={() => setActive(m.id)}
                  className={`flex h-full w-full flex-col rounded-2xl border p-6 text-left transition ${
                    on
                      ? "border-neon-cyan/60 bg-ink-900/90 shadow-[0_0_40px_-12px_rgba(34,211,238,0.5)]"
                      : "border-white/10 bg-ink-900/50 hover:border-white/25"
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <span className="grid h-11 w-11 place-items-center rounded-xl bg-gradient-to-br from-neon-cyan/20 to-neon-violet/20 text-neon-cyan">
                      <Icon className="h-5 w-5" />
                    </span>
                    <span
                      className={`rounded-full px-2.5 py-1 text-[11px] font-semibold ${
                        m.highlight
                          ? "bg-gradient-to-r from-neon-cyan to-neon-violet text-ink-950"
                          : "bg-white/10 text-slate-300"
                      }`}
                    >
                      {m.badge}
                    </span>
                  </div>
                  <h3 className="mt-4 text-lg font-bold text-white">{m.name}</h3>
                  <p className="mt-1 text-sm text-slate-400">{m.tagline}</p>

                  <div className="mt-4 space-y-1.5 text-xs">
                    <p className="text-slate-400">
                      <span className="text-slate-500">{e.youLabel}：</span>
                      {m.you}
                    </p>
                    <p className="text-slate-400">
                      <span className="text-slate-500">{e.weLabel}：</span>
                      {m.we}
                    </p>
                  </div>

                  <ul className="mt-4 space-y-2">
                    {m.points.map((p) => (
                      <li key={p} className="flex items-start gap-2 text-sm text-slate-300">
                        <Check className="mt-0.5 h-4 w-4 shrink-0 text-neon-cyan" />
                        {p}
                      </li>
                    ))}
                  </ul>

                  <div className="mt-auto pt-5">
                    <p className="text-xl font-bold text-white">{m.price}</p>
                    <p className="mt-1 text-[11px] text-slate-500">{m.priceNote}</p>
                    <span className="group mt-4 inline-flex items-center gap-1.5 text-sm font-medium text-neon-cyan">
                      {m.cta}
                      <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-1" />
                    </span>
                  </div>
                </button>
              </Reveal>
            );
          })}
        </div>

        {/* detail panel */}
        <Reveal className="mt-8">
          {active === "service" && (
            <div className="rounded-2xl border border-white/10 bg-ink-900/40 p-6">
              <h4 className="mb-4 text-sm font-semibold text-slate-300">{e.serviceTiersLabel}</h4>
              <div className="grid gap-4 md:grid-cols-3">
                {rt.plans.map((p) => (
                  <div
                    key={p.name}
                    className={`rounded-xl border p-5 ${
                      p.highlight ? "border-neon-cyan/40 bg-neon-cyan/[0.04]" : "border-white/10 bg-ink-900/60"
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-semibold text-white">{p.name}</span>
                      {p.tag && (
                        <span className="rounded-full bg-white/10 px-2 py-0.5 text-[10px] text-slate-300">{p.tag}</span>
                      )}
                    </div>
                    <p className="mt-2 text-lg font-bold text-neon-cyan">{p.price}</p>
                    <p className="text-[11px] text-slate-500">{p.unit}</p>
                    <ul className="mt-3 space-y-1.5">
                      {p.specs.map((s) => (
                        <li key={s} className="flex items-start gap-1.5 text-xs text-slate-400">
                          <Check className="mt-0.5 h-3.5 w-3.5 shrink-0 text-neon-cyan" />
                          {s}
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
              </div>
              <h4 className="mb-3 mt-6 text-sm font-semibold text-slate-300">{e.extrasLabel}</h4>
              <div className="flex flex-wrap gap-2">
                {rt.extras.map((x) => (
                  <span key={x} className="rounded-full border border-white/10 bg-white/5 px-3 py-1.5 text-xs text-slate-300">
                    {x}
                  </span>
                ))}
              </div>
            </div>
          )}

          {active === "managed" && (
            <div className="rounded-2xl border border-neon-cyan/20 bg-ink-900/40 p-6">
              <div className="grid gap-4 md:grid-cols-2">
                {e.models[1].points.map((p) => (
                  <div key={p} className="flex items-start gap-2.5 rounded-xl border border-white/10 bg-ink-900/60 p-4 text-sm text-slate-300">
                    <Check className="mt-0.5 h-4 w-4 shrink-0 text-neon-cyan" />
                    {p}
                  </div>
                ))}
              </div>
              <p className="mt-4 text-center text-sm text-slate-400">
                {e.models[1].price} · {e.models[1].priceNote}
              </p>
            </div>
          )}

          {active === "invest" && (
            <div className="grid gap-6 lg:grid-cols-2">
              <div className="rounded-2xl border border-neon-violet/25 bg-ink-900/40 p-6">
                <h4 className="mb-4 text-sm font-semibold text-slate-300">{e.invest.roiTitle}</h4>
                <div className="space-y-2.5">
                  {e.invest.roiRows.map((r) => (
                    <div key={r.label} className="flex items-center justify-between border-b border-white/5 pb-2.5 text-sm">
                      <span className="text-slate-400">{r.label}</span>
                      <span className="font-semibold text-neon-cyan">{r.value}</span>
                    </div>
                  ))}
                </div>
                <p className="mt-4 text-[11px] leading-relaxed text-slate-500">{e.invest.roiNote}</p>
              </div>
              <div className="rounded-2xl border border-white/10 bg-ink-900/40 p-6">
                <h4 className="mb-4 text-sm font-semibold text-slate-300">{e.invest.flowTitle}</h4>
                <ol className="space-y-3">
                  {e.invest.flow.map((f, i) => (
                    <li key={f} className="flex items-start gap-3 text-sm text-slate-300">
                      <span className="grid h-6 w-6 shrink-0 place-items-center rounded-full bg-gradient-to-br from-neon-cyan to-neon-violet text-[11px] font-bold text-ink-950">
                        {i + 1}
                      </span>
                      {f}
                    </li>
                  ))}
                </ol>
                <p className="mt-5 rounded-lg border border-white/10 bg-white/5 p-3 text-[11px] leading-relaxed text-slate-400">
                  {e.invest.compliance}
                </p>
              </div>
            </div>
          )}
        </Reveal>

        {/* comparison matrix */}
        <Reveal className="mt-16">
          <h3 className="mb-5 text-center text-xl font-bold text-white">{e.matrixTitle}</h3>

          {/* Mobile: stacked cards */}
          <div className="space-y-3 md:hidden">
            {e.matrix.map((row) => (
              <div key={row.label} className="rounded-2xl border border-white/10 bg-ink-900/60 p-4">
                <div className="text-sm font-semibold text-white">{row.label}</div>
                <div className="mt-3 space-y-2 text-sm">
                  <div className="flex items-start justify-between gap-3 px-1">
                    <span className="text-xs text-slate-500">{e.matrixCols[0]}</span>
                    <span className="text-right text-slate-400">{row.a}</span>
                  </div>
                  <div className="flex items-start justify-between gap-3 rounded-lg bg-neon-cyan/[0.06] px-3 py-1.5">
                    <span className="text-xs font-medium text-neon-cyan">{e.matrixCols[1]}</span>
                    <span className="text-right font-medium text-neon-cyan/90">{row.b}</span>
                  </div>
                  <div className="flex items-start justify-between gap-3 px-1">
                    <span className="text-xs text-slate-500">{e.matrixCols[2]}</span>
                    <span className="text-right text-slate-400">{row.c}</span>
                  </div>
                </div>
              </div>
            ))}
          </div>

          {/* Desktop / tablet: matrix table */}
          <div className="hidden md:block">
            <div className="overflow-hidden rounded-2xl border border-white/10 bg-ink-900/60">
              <div className="grid grid-cols-[1.2fr_1fr_1fr_1fr] border-b border-white/10 bg-ink-800/50 text-sm">
                <span className="px-4 py-3" />
                {e.matrixCols.map((c, i) => (
                  <span
                    key={c}
                    className={`px-4 py-3 text-center font-semibold ${
                      i === 1 ? "text-neon-cyan" : "text-white"
                    }`}
                  >
                    {c}
                  </span>
                ))}
              </div>
              {e.matrix.map((row, ri) => (
                <div
                  key={row.label}
                  className={`grid grid-cols-[1.2fr_1fr_1fr_1fr] text-sm ${
                    ri % 2 ? "bg-white/[0.02]" : ""
                  }`}
                >
                  <span className="px-4 py-3 font-medium text-slate-300">{row.label}</span>
                  <span className="px-4 py-3 text-center text-slate-400">{row.a}</span>
                  <span className="px-4 py-3 text-center text-neon-cyan/90">{row.b}</span>
                  <span className="px-4 py-3 text-center text-slate-400">{row.c}</span>
                </div>
              ))}
            </div>
          </div>
        </Reveal>
      </div>
    </section>
  );
}
