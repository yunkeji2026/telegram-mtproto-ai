"use client";

import { Radio, Globe, Clapperboard, Building2, ArrowRight, LucideIcon } from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import { track } from "@/lib/track";

const ICONS: Record<string, LucideIcon> = {
  streamer: Radio,
  ecom: Globe,
  creator: Clapperboard,
  enterprise: Building2,
};

export default function Personas() {
  const { t } = useLang();
  const p = t.personas;

  return (
    <section className="relative py-16">
      <div className="mx-auto max-w-6xl px-5">
        <Reveal className="mb-10 text-center">
          <h2 className="text-2xl font-bold text-white md:text-3xl">{p.title}</h2>
          <p className="mx-auto mt-3 max-w-2xl text-slate-400">{p.subtitle}</p>
        </Reveal>

        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {p.items.map((it, i) => {
            const Icon = ICONS[it.id] ?? Radio;
            return (
              <Reveal key={it.id} delay={i * 0.06} className="h-full">
                <a
                  href={it.href}
                  onClick={() => track("persona_click", { id: it.id })}
                  className="group flex h-full flex-col rounded-2xl border border-white/10 bg-ink-900/60 p-6 transition hover:-translate-y-1 hover:border-neon-cyan/40 hover:bg-ink-900/80"
                >
                  <span className="grid h-12 w-12 place-items-center rounded-xl bg-gradient-to-br from-neon-cyan/20 to-neon-violet/20 text-neon-cyan transition group-hover:scale-105">
                    <Icon className="h-6 w-6" />
                  </span>
                  <h3 className="mt-4 font-semibold text-white">{it.title}</h3>
                  <p className="mt-2 flex-1 text-sm leading-relaxed text-slate-400">{it.desc}</p>
                  <span className="mt-4 inline-flex items-center gap-1.5 text-sm font-medium text-neon-cyan">
                    {it.cta}
                    <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-1" />
                  </span>
                </a>
              </Reveal>
            );
          })}
        </div>
      </div>
    </section>
  );
}
