"use client";

import { motion } from "framer-motion";
import { Cloud, ServerCog, Check, X, Lock } from "lucide-react";
import { useLang } from "./LanguageContext";

export default function DeployDiagram() {
  const { t } = useLang();
  const d = t.deployDemo;

  return (
    <div className="relative mx-auto w-full max-w-[440px]">
      <div className="pointer-events-none absolute -inset-4 -z-10 rounded-3xl bg-gradient-to-br from-emerald-400/10 to-neon-cyan/10 blur-2xl" />
      <div className="overflow-hidden rounded-2xl border border-white/10 bg-ink-900/80 shadow-2xl">
        {/* column headers */}
        <div className="grid grid-cols-[1fr_5.5rem_7rem] items-center gap-2 border-b border-white/10 bg-ink-800/50 px-4 py-3">
          <span aria-hidden />
          <span className="inline-flex items-center justify-center gap-1 justify-self-center whitespace-nowrap rounded-full border border-white/10 bg-white/[0.04] px-2.5 py-1 text-[11px] font-medium text-slate-400">
            <Cloud className="h-3.5 w-3.5 shrink-0" />
            {d.cloudLabel}
          </span>
          <span className="inline-flex items-center justify-center gap-1 justify-self-center whitespace-nowrap rounded-full border border-emerald-400/40 bg-emerald-400/15 px-2.5 py-1 text-[11px] font-semibold text-emerald-300 shadow-[0_0_18px_-6px_rgba(16,185,129,0.6)]">
            <ServerCog className="h-3.5 w-3.5 shrink-0" />
            {d.localLabel}
          </span>
        </div>

        {/* rows */}
        <div className="divide-y divide-white/5">
          {d.rows.map((r, i) => (
            <motion.div
              key={r.label}
              initial={{ opacity: 0, x: -8 }}
              whileInView={{ opacity: 1, x: 0 }}
              viewport={{ once: true }}
              transition={{ delay: i * 0.06 }}
              className="grid grid-cols-[1fr_5.5rem_7rem] items-center gap-2 px-4 py-3"
            >
              <span className="text-sm text-slate-200">{r.label}</span>
              <span className="flex justify-center">
                {r.cloud ? (
                  <Check className="h-4 w-4 text-slate-400" />
                ) : (
                  <X className="h-4 w-4 text-rose-400/70" />
                )}
              </span>
              <span className="flex justify-center">
                {r.local ? (
                  <span className="grid h-5 w-5 place-items-center rounded-full bg-emerald-400/20">
                    <Check className="h-3.5 w-3.5 text-emerald-300" />
                  </span>
                ) : (
                  <X className="h-4 w-4 text-rose-400/70" />
                )}
              </span>
            </motion.div>
          ))}
        </div>

        {/* footer */}
        <div className="flex items-center gap-2 border-t border-white/10 bg-emerald-400/5 px-4 py-3 text-[11px] text-emerald-300/90">
          <Lock className="h-3.5 w-3.5 shrink-0" />
          {t.deployDemo.features[1]} · {t.deployDemo.features[3]}
        </div>
      </div>
    </div>
  );
}
