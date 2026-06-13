"use client";

import { useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import { track } from "@/lib/track";

export default function Pricing() {
  const { t } = useLang();
  const [cat, setCat] = useState("all");
  const filtered =
    cat === "all" ? t.solutions : t.solutions.filter((s) => s.id === cat);

  return (
    <section id="pricing" className="relative py-24">
      <div className="pointer-events-none absolute left-1/2 top-20 h-80 w-80 -translate-x-1/2 rounded-full bg-neon-blue/15 blur-[130px]" />
      <div className="relative mx-auto max-w-7xl px-5">
        <Reveal className="mb-12 text-center">
          <h2 className="text-3xl font-bold text-white md:text-4xl">{t.pricingSection.title}</h2>
          <p className="mx-auto mt-3 max-w-2xl text-slate-400">{t.pricingSection.subtitle}</p>
          <span className="mt-4 inline-block rounded-full border border-neon-cyan/30 bg-neon-cyan/10 px-3 py-1 text-xs text-neon-cyan">
            {t.pricingSection.unit}
          </span>
        </Reveal>
      </div>

      <div className="relative mx-auto max-w-7xl px-5">
        {/* category selector */}
        <div className="mb-8 flex flex-wrap justify-center gap-2">
          <button
            onClick={() => setCat("all")}
            className={`rounded-full px-4 py-1.5 text-sm transition ${
              cat === "all"
                ? "bg-gradient-to-r from-neon-cyan to-neon-violet text-ink-950"
                : "border border-white/10 text-slate-400 hover:border-neon-cyan/40 hover:text-white"
            }`}
          >
            {t.pricingSection.allLabel}
          </button>
          {t.solutions.map((s) => (
            <button
              key={s.id}
              onClick={() => {
                setCat(s.id);
                track("pricing_filter", { id: s.id });
              }}
              className={`rounded-full px-4 py-1.5 text-sm transition ${
                cat === s.id
                  ? "bg-gradient-to-r from-neon-cyan to-neon-violet text-ink-950"
                  : "border border-white/10 text-slate-400 hover:border-neon-cyan/40 hover:text-white"
              }`}
            >
              {s.tag}
            </button>
          ))}
        </div>

        <motion.div layout className="grid gap-6 lg:grid-cols-2">
          <AnimatePresence mode="popLayout">
          {filtered.map((s, i) => (
            <motion.div
              key={s.id}
              layout
              initial={{ opacity: 0, y: 24 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.97 }}
              transition={{ duration: 0.35, delay: (i % 2) * 0.05 }}
              className="overflow-hidden rounded-2xl border border-white/10 bg-ink-900/60"
            >
              <div className="flex items-center justify-between border-b border-white/5 px-5 py-4">
                <h3 className="font-semibold text-white">{s.title}</h3>
                <span className="rounded-full border border-white/10 px-2.5 py-0.5 text-[11px] text-slate-400">
                  {s.tag}
                </span>
              </div>
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-xs text-slate-500">
                    <th className="px-5 py-2 font-medium">{t.pricingSection.planCol}</th>
                    <th className="px-5 py-2 font-medium">{t.pricingSection.priceCol}</th>
                    <th className="px-5 py-2 font-medium">{t.pricingSection.detailCol}</th>
                  </tr>
                </thead>
                <tbody>
                  {s.pricing.map((p) => (
                    <tr key={p.plan} className="border-t border-white/5">
                      <td className="px-5 py-2.5 text-slate-300">{p.plan}</td>
                      <td className="whitespace-nowrap px-5 py-2.5 font-semibold text-neon-cyan">{p.price}</td>
                      <td className="px-5 py-2.5 text-slate-400">{p.detail}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </motion.div>
          ))}
          </AnimatePresence>
        </motion.div>

        <p className="mx-auto mt-8 max-w-3xl text-center text-xs text-slate-500">{t.pricingSection.note}</p>
      </div>
    </section>
  );
}
