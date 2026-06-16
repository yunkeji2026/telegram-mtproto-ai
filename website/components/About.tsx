"use client";

import { motion } from "framer-motion";
import { Cpu, Globe2, ShieldCheck, Zap, LucideIcon } from "lucide-react";
import { useLang } from "./LanguageContext";

const ICONS: LucideIcon[] = [Cpu, Globe2, ShieldCheck, Zap];

export default function About() {
  const { t } = useLang();

  return (
    <section id="about" className="relative py-24">
      <div className="mx-auto max-w-7xl px-5">
        <div className="mb-12 text-center">
          <h2 className="text-3xl font-bold text-white md:text-4xl">{t.about.title}</h2>
          <p className="mx-auto mt-3 max-w-2xl text-slate-400">{t.about.subtitle}</p>
        </div>

        <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-4">
          {t.about.points.map((p, i) => {
            const Icon = ICONS[i] ?? Cpu;
            return (
              <motion.div
                key={p.title}
                initial={{ opacity: 0, y: 24 }}
                whileInView={{ opacity: 1, y: 0 }}
                viewport={{ once: true, margin: "-60px" }}
                transition={{ duration: 0.45, delay: i * 0.07 }}
                className="card-hover rounded-2xl border border-white/10 bg-ink-900/60 p-6"
              >
                <span className="grid h-11 w-11 place-items-center rounded-xl bg-gradient-to-br from-neon-cyan/20 to-neon-violet/20 text-neon-cyan">
                  <Icon className="h-5 w-5" />
                </span>
                <h3 className="mt-4 font-semibold text-white">{p.title}</h3>
                <p className="mt-2 text-sm leading-relaxed text-slate-400">{p.desc}</p>
              </motion.div>
            );
          })}
        </div>
      </div>
    </section>
  );
}
