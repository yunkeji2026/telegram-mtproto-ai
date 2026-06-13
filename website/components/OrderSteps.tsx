"use client";

import { MousePointerClick, MessagesSquare, Wallet } from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";

const ICONS = [MousePointerClick, MessagesSquare, Wallet];

export default function OrderSteps() {
  const { t } = useLang();

  return (
    <section className="relative py-24">
      <div className="mx-auto max-w-7xl px-5">
        <div className="mb-12 text-center">
          <h2 className="text-3xl font-bold text-white md:text-4xl">{t.orderSteps.title}</h2>
          <p className="mx-auto mt-3 max-w-2xl text-slate-400">{t.orderSteps.subtitle}</p>
        </div>

        <div className="relative grid gap-6 md:grid-cols-3">
          <div className="pointer-events-none absolute left-0 right-0 top-12 hidden h-px bg-gradient-to-r from-transparent via-neon-violet/40 to-transparent md:block" />
          {t.orderSteps.steps.map((s, i) => {
            const Icon = ICONS[i] ?? MousePointerClick;
            return (
              <Reveal key={s.title} delay={i * 0.1}>
                <div className="card-hover relative rounded-2xl border border-white/10 bg-ink-900/60 p-6 text-center">
                  <div className="mx-auto grid h-14 w-14 place-items-center rounded-2xl bg-gradient-to-br from-neon-cyan/20 to-neon-violet/20 text-neon-cyan">
                    <Icon className="h-6 w-6" />
                  </div>
                  <span className="mt-4 inline-block rounded-full border border-white/10 px-3 py-0.5 text-xs text-slate-400">
                    {String(i + 1).padStart(2, "0")}
                  </span>
                  <h3 className="mt-3 font-semibold text-white">{s.title}</h3>
                  <p className="mt-2 text-sm leading-relaxed text-slate-400">{s.desc}</p>
                </div>
              </Reveal>
            );
          })}
        </div>
      </div>
    </section>
  );
}
