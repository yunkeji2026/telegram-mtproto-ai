"use client";

import { motion } from "framer-motion";
import { Zap, Monitor, Cpu, ShieldCheck, Sparkles } from "lucide-react";
import { useLang } from "./LanguageContext";
import { CONTACT_URL } from "@/lib/site";
import { track } from "@/lib/track";
import Reveal from "./fx/Reveal";
import LiveSwapStage from "./LiveSwapStage";

const ICONS: Record<string, typeof Zap> = {
  zap: Zap,
  monitor: Monitor,
  cpu: Cpu,
  shield: ShieldCheck,
};

export default function RealtimeSwap() {
  const { t } = useLang();
  const rt = t.realtime;

  return (
    <section id="realtime" className="relative py-24">
      <div className="mx-auto max-w-6xl px-5">
        <Reveal>
          <div className="mb-12 text-center">
            <div className="mb-2 flex items-center justify-center gap-3 text-xs font-semibold uppercase tracking-[0.35em] text-neon-cyan/50">
              <span className="h-px w-8 bg-neon-cyan/30" />
              02 · Flagship
              <span className="h-px w-8 bg-neon-cyan/30" />
            </div>
            <span className="inline-flex items-center gap-1.5 rounded-full border border-neon-violet/40 bg-neon-violet/10 px-3 py-1 text-xs font-medium text-neon-violet">
              <Sparkles className="h-3.5 w-3.5" />
              {rt.badge}
            </span>
            <h2 className="mt-4 text-3xl font-bold text-white md:text-4xl">{rt.title}</h2>
            <p className="mx-auto mt-4 max-w-3xl text-slate-400">{rt.subtitle}</p>
          </div>
        </Reveal>

        {/* Live video-call swap stage */}
        <Reveal>
          <div className="mx-auto mb-16 h-[380px] max-w-xl sm:h-[460px]">
            <LiveSwapStage />
          </div>
        </Reveal>

        {/* Features */}
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {rt.features.map((f, i) => {
            const Icon = ICONS[f.icon] ?? Zap;
            return (
              <Reveal key={f.title} delay={i * 0.05}>
                <div className="group h-full rounded-2xl border border-white/10 bg-ink-900/60 p-6 transition hover:border-neon-cyan/40 hover:bg-ink-900/80">
                  <span className="inline-grid h-12 w-12 place-items-center rounded-xl bg-gradient-to-br from-neon-cyan/20 to-neon-violet/20 text-neon-cyan transition group-hover:scale-105">
                    <Icon className="h-6 w-6" />
                  </span>
                  <h3 className="mt-4 font-semibold text-white">{f.title}</h3>
                  <p className="mt-1.5 text-sm leading-relaxed text-slate-400">{f.desc}</p>
                </div>
              </Reveal>
            );
          })}
        </div>

        {/* Steps */}
        <Reveal>
          <div className="mt-16">
            <h3 className="mb-8 text-center text-xl font-semibold text-white">{rt.stepsTitle}</h3>
            <div className="grid gap-4 md:grid-cols-4">
              {rt.steps.map((s, i) => (
                <div key={s.title} className="relative rounded-2xl border border-white/10 bg-ink-900/40 p-5">
                  <div className="flex h-9 w-9 items-center justify-center rounded-full bg-gradient-to-br from-neon-cyan to-neon-violet text-sm font-bold text-ink-950">
                    {i + 1}
                  </div>
                  <h4 className="mt-3 font-medium text-white">{s.title}</h4>
                  <p className="mt-1 text-sm text-slate-400">{s.desc}</p>
                </div>
              ))}
            </div>
          </div>
        </Reveal>

        {/* Recommended hardware (client buys) */}
        <Reveal>
          <div className="mt-16">
            <div className="mb-2 text-center">
              <h3 className="text-2xl font-bold text-white">{rt.hardwareTitle}</h3>
              <p className="mx-auto mt-2 max-w-2xl text-sm text-slate-400">{rt.hardwareNote}</p>
            </div>
            <div className="mt-8 grid gap-4 md:grid-cols-3">
              {rt.hardware.map((h, i) => (
                <div
                  key={h.tier}
                  className="rounded-2xl border border-white/10 bg-ink-900/50 p-5 transition hover:border-neon-cyan/30"
                >
                  <div className="flex items-center gap-2">
                    <span className="grid h-7 w-7 place-items-center rounded-lg bg-gradient-to-br from-neon-blue/30 to-neon-violet/30 text-xs font-bold text-neon-cyan">
                      {i + 1}
                    </span>
                    <span className="text-sm font-semibold text-white">{h.tier}</span>
                  </div>
                  <div className="mt-3 font-mono text-sm text-neon-cyan">{h.gpu}</div>
                  <p className="mt-2 text-xs leading-relaxed text-slate-400">{h.use}</p>
                </div>
              ))}
            </div>
          </div>
        </Reveal>

        {/* capacity note + CTA to engagement models */}
        <Reveal>
          <div className="mt-16 text-center">
            <p className="mx-auto max-w-2xl text-sm text-slate-400">{rt.capacityNote}</p>
            <div className="mt-6 flex flex-wrap justify-center gap-3">
              <a
                href="#engage"
                onClick={() => track("cta_click", { where: "realtime_main", which: "engage" })}
                className="rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-8 py-3.5 text-sm font-semibold text-ink-950 transition hover:opacity-90"
              >
                {rt.plansTitle}
              </a>
              <motion.a
                whileTap={{ scale: 0.97 }}
                href={CONTACT_URL}
                target="_blank"
                rel="noreferrer"
                onClick={() => track("cta_click", { where: "realtime_main", which: "contact" })}
                className="rounded-full border border-white/15 px-8 py-3.5 text-sm font-semibold text-white transition hover:bg-white/5"
              >
                {rt.cta}
              </motion.a>
            </div>
          </div>
        </Reveal>
      </div>
    </section>
  );
}
