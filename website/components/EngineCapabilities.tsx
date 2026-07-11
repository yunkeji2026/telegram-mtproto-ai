"use client";

import {
  Mic,
  ScanFace,
  Sparkles,
  Palette,
  Users,
  Languages,
  Subtitles,
  Gauge,
  Brain,
  Wand2,
  Images,
  Fingerprint,
  ArrowRight,
} from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import { ENGINE } from "@/lib/engineContent";
import { CONTACT_URL } from "@/lib/site";
import { track } from "@/lib/track";

const ICONS: Record<string, typeof Mic> = {
  mic: Mic,
  scanface: ScanFace,
  sparkles: Sparkles,
  palette: Palette,
  users: Users,
  languages: Languages,
  subtitles: Subtitles,
  gauge: Gauge,
  brain: Brain,
  palette2: Wand2,
  image: Images,
  fingerprint: Fingerprint,
};

export default function EngineCapabilities() {
  const { lang } = useLang();
  const c = ENGINE.caps;

  return (
    <section id="engine" className="relative py-24">
      <div className="pointer-events-none absolute left-1/2 top-0 -z-10 h-72 w-[820px] -translate-x-1/2 rounded-full bg-neon-violet/10 blur-[130px]" />
      <div className="mx-auto max-w-7xl px-5">
        <Reveal className="mx-auto max-w-3xl text-center">
          <p className="text-xs font-medium uppercase tracking-[0.28em] text-neon-cyan">{c.kicker[lang]}</p>
          <h2 className="mt-3 text-3xl font-bold text-white md:text-4xl">{c.title[lang]}</h2>
          <p className="mt-4 text-base text-slate-400">{c.subtitle[lang]}</p>
        </Reveal>

        <div className="mt-12 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {c.items.map((it, idx) => {
            const Icon = ICONS[it.icon] ?? Sparkles;
            return (
              <Reveal key={it.title.en} delay={(idx % 3) * 0.05}>
                <div className="card-hover group relative flex h-full flex-col overflow-hidden rounded-2xl border border-white/10 bg-white/[0.03] p-5">
                  <div className="mb-4 flex items-center justify-between">
                    <span className="inline-grid h-11 w-11 place-items-center rounded-xl bg-gradient-to-br from-neon-cyan/20 to-neon-violet/20 text-neon-cyan transition group-hover:scale-105">
                      <Icon className="h-5 w-5" />
                    </span>
                    {it.badge && (
                      <span className="rounded-full border border-neon-violet/40 bg-neon-violet/10 px-2 py-0.5 text-[10px] font-semibold text-neon-violet">
                        {it.badge[lang]}
                      </span>
                    )}
                  </div>
                  <span className="text-[11px] font-medium text-neon-cyan/80">{it.line[lang]}</span>
                  <h3 className="mt-1 text-lg font-bold text-white">{it.title[lang]}</h3>
                  <p className="mt-2 flex-1 text-sm leading-relaxed text-slate-300">{it.desc[lang]}</p>
                  <p className="mt-4 inline-flex w-fit items-center gap-1.5 rounded-lg border border-white/10 bg-black/30 px-2.5 py-1 font-mono text-[11px] text-emerald-300/90">
                    <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
                    {it.proof[lang]}
                  </p>
                </div>
              </Reveal>
            );
          })}
        </div>

        <Reveal className="mt-8 text-center">
          <p className="mx-auto max-w-3xl text-[11px] leading-relaxed text-slate-500">{c.footnote[lang]}</p>
          <a
            href={CONTACT_URL}
            target="_blank"
            rel="noreferrer"
            onClick={() => track("cta_click", { where: "engine_caps" })}
            className="group mt-6 inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-6 py-3 text-sm font-semibold text-ink-950 transition hover:opacity-90"
          >
            {lang === "zh" ? "要一份能力↔证据对照清单" : "Get the capability-to-evidence sheet"}
            <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-1" />
          </a>
        </Reveal>
      </div>
    </section>
  );
}
