"use client";

import { Languages, Bot, Mic, Inbox, ArrowRight, Check, X, type LucideIcon } from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import { track } from "@/lib/track";
import AutoChatDemo from "./AutoChatDemo";
import Plans from "./Plans";
import RoiCalculator from "./RoiCalculator";

const ICONS: Record<string, LucideIcon> = {
  languages: Languages,
  bot: Bot,
  mic: Mic,
  inbox: Inbox,
};

export default function AutoChat() {
  const { t } = useLang();
  const a = t.autochat;

  return (
    <section id="autochat" className="relative overflow-hidden py-24">
      <div className="pointer-events-none absolute left-1/2 top-0 -z-10 h-[420px] w-[820px] -translate-x-1/2 rounded-full bg-neon-cyan/10 blur-[120px]" />
      <div className="mx-auto max-w-6xl px-5">
        <Reveal className="mb-14 text-center">
          <div className="mb-2 flex items-center justify-center gap-3 text-xs font-semibold uppercase tracking-[0.35em] text-neon-violet/50">
            <span className="h-px w-8 bg-neon-violet/30" />
            01 · Flagship
            <span className="h-px w-8 bg-neon-violet/30" />
          </div>
          <span className="inline-flex items-center gap-1.5 rounded-full border border-neon-cyan/30 bg-neon-cyan/10 px-3 py-1 text-xs font-medium text-neon-cyan">
            {a.badge}
          </span>
          <h2 className="mt-4 text-3xl font-bold text-white md:text-4xl">{a.title}</h2>
          <p className="mx-auto mt-4 max-w-3xl text-slate-400">{a.subtitle}</p>
        </Reveal>

        <div className="grid items-center gap-12 lg:grid-cols-2">
          <Reveal>
            <AutoChatDemo />
          </Reveal>

          <Reveal delay={0.1}>
            <div className="grid gap-4 sm:grid-cols-2">
              {a.features.map((f) => {
                const Icon = ICONS[f.icon] ?? Bot;
                return (
                  <div
                    key={f.title}
                    className="rounded-2xl border border-white/10 bg-ink-900/60 p-5 transition hover:border-neon-cyan/40 hover:bg-ink-900/80"
                  >
                    <span className="grid h-11 w-11 place-items-center rounded-xl bg-gradient-to-br from-neon-cyan/20 to-neon-violet/20 text-neon-cyan">
                      <Icon className="h-5 w-5" />
                    </span>
                    <h3 className="mt-4 font-semibold text-white">{f.title}</h3>
                    <p className="mt-2 text-sm leading-relaxed text-slate-400">{f.desc}</p>
                  </div>
                );
              })}
            </div>

            <div className="mt-6 flex flex-wrap items-center gap-2">
              <span className="text-sm text-slate-400">{a.scenariosLabel}:</span>
              {a.scenarios.map((s) => (
                <span
                  key={s}
                  className="rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs text-slate-300"
                >
                  {s}
                </span>
              ))}
            </div>

            <a
              href="#contact"
              onClick={() => track("cta_click", { where: "autochat", which: "main" })}
              className="group mt-6 inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-6 py-3 text-sm font-semibold text-ink-950 transition hover:opacity-90"
            >
              {a.cta}
              <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-1" />
            </a>
          </Reveal>
        </div>

        {/* AI vs ordinary translation comparison */}
        <Reveal className="mt-20">
          <div className="mx-auto max-w-4xl">
            <div className="mb-2 text-center">
              <h3 className="text-2xl font-bold text-white">{a.compareTitle}</h3>
              <p className="mx-auto mt-2 max-w-2xl text-sm text-slate-400">{a.compareNote}</p>
            </div>
            <div className="mt-6 space-y-4">
              {a.compare.map((c) => (
                <div
                  key={c.src}
                  className="overflow-hidden rounded-2xl border border-white/10 bg-ink-900/60"
                >
                  <div className="border-b border-white/10 bg-ink-800/50 px-4 py-2.5 text-sm text-slate-300">
                    <span className="mr-2 rounded bg-white/10 px-1.5 py-0.5 text-[10px] font-medium text-slate-400">
                      原文 SRC
                    </span>
                    {c.src}
                  </div>
                  <div className="grid sm:grid-cols-2">
                    <div className="flex items-start gap-2 border-b border-white/10 p-4 sm:border-b-0 sm:border-r">
                      <X className="mt-0.5 h-4 w-4 shrink-0 text-red-400/80" />
                      <div>
                        <span className="block text-[11px] font-medium text-red-400/80">{a.badLabel}</span>
                        <p className="mt-1 text-sm text-slate-400 line-through decoration-red-400/40">{c.bad}</p>
                      </div>
                    </div>
                    <div className="flex items-start gap-2 bg-neon-cyan/[0.04] p-4">
                      <Check className="mt-0.5 h-4 w-4 shrink-0 text-emerald-400" />
                      <div>
                        <span className="block text-[11px] font-medium text-emerald-400">{a.goodLabel}</span>
                        <p className="mt-1 text-sm font-medium text-white">{c.good}</p>
                      </div>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </Reveal>

        {/* AI auto-closing chat plans */}
        <Reveal className="mt-20">
          <Plans />
        </Reveal>
      </div>

      {/* ROI calculator */}
      <RoiCalculator />
    </section>
  );
}
