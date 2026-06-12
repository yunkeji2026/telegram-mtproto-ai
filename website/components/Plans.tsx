"use client";

import { useState } from "react";
import { Check } from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import BorderBeam from "./fx/BorderBeam";
import Magnetic from "./fx/Magnetic";
import { CONTACT_URL } from "@/lib/site";

export default function Plans() {
  const { t } = useLang();
  const [yearly, setYearly] = useState(false);

  return (
    <div className="mx-auto max-w-7xl px-5">
      <div className="mb-8 text-center">
        <h3 className="text-2xl font-bold text-white md:text-3xl">{t.plans.title}</h3>
        <p className="mx-auto mt-3 max-w-xl text-slate-400">{t.plans.subtitle}</p>

        {/* billing toggle */}
        <div className="mt-6 inline-flex items-center gap-1 rounded-full border border-white/10 bg-ink-900/60 p-1 text-sm">
          <button
            onClick={() => setYearly(false)}
            className={`rounded-full px-4 py-1.5 transition ${
              !yearly ? "bg-gradient-to-r from-neon-cyan to-neon-violet text-ink-950" : "text-slate-400"
            }`}
          >
            {t.plans.monthly}
          </button>
          <button
            onClick={() => setYearly(true)}
            className={`flex items-center gap-2 rounded-full px-4 py-1.5 transition ${
              yearly ? "bg-gradient-to-r from-neon-cyan to-neon-violet text-ink-950" : "text-slate-400"
            }`}
          >
            {t.plans.yearly}
            <span
              className={`rounded-full px-1.5 py-0.5 text-[10px] ${
                yearly ? "bg-ink-950/20 text-ink-950" : "bg-neon-cyan/15 text-neon-cyan"
              }`}
            >
              {t.plans.save}
            </span>
          </button>
        </div>
      </div>

      <div className="grid items-stretch gap-6 md:grid-cols-3">
        {t.plans.items.map((p, i) => (
          <Reveal key={p.name} delay={i * 0.08} className="h-full">
            <div
              className={`card-hover relative flex h-full flex-col overflow-hidden rounded-2xl border p-6 ${
                p.highlight
                  ? "border-transparent bg-gradient-to-b from-neon-violet/15 to-ink-900/60 shadow-[0_0_40px_-12px_rgba(139,92,246,0.5)]"
                  : "border-white/10 bg-ink-900/60"
              }`}
            >
              {p.highlight && <BorderBeam />}
              {p.highlight && (
                <span className="absolute right-4 top-4 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-2.5 py-0.5 text-[10px] font-semibold text-ink-950">
                  {t.plans.popular}
                </span>
              )}

              <h4 className="text-lg font-semibold text-white">{p.name}</h4>
              <p className="mt-1 text-xs text-slate-400">{p.desc}</p>

              <div className="mt-5 flex items-end gap-1">
                <span className="text-xs text-slate-500">USDT</span>
                <span className="text-4xl font-bold tabular-nums text-white">
                  {yearly ? p.priceYearly : p.priceMonthly}
                </span>
                <span className="mb-1 text-sm text-slate-400">{t.plans.perMonth}</span>
              </div>

              <ul className="mt-6 flex-1 space-y-2.5">
                {p.features.map((f) => (
                  <li key={f} className="flex items-start gap-2 text-sm text-slate-300">
                    <Check className="mt-0.5 h-4 w-4 shrink-0 text-neon-cyan" />
                    {f}
                  </li>
                ))}
              </ul>

              <Magnetic className="mt-6 w-full">
                <a
                  href={CONTACT_URL}
                  target="_blank"
                  rel="noreferrer"
                  className={`block w-full rounded-full px-5 py-2.5 text-center text-sm font-medium transition ${
                    p.highlight
                      ? "bg-gradient-to-r from-neon-cyan to-neon-violet text-ink-950 hover:opacity-90"
                      : "border border-white/15 text-slate-200 hover:border-neon-cyan/50 hover:text-white"
                  }`}
                >
                  {t.plans.cta}
                </a>
              </Magnetic>
            </div>
          </Reveal>
        ))}
      </div>
    </div>
  );
}
