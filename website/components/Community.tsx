"use client";

import { motion } from "framer-motion";
import { Send, Check, Users } from "lucide-react";
import { useLang } from "./LanguageContext";
import { CHANNEL_URL, GROUP_URL } from "@/lib/site";
import { track } from "@/lib/track";
import Reveal from "./fx/Reveal";

export default function Community() {
  const { t } = useLang();
  const c = t.community;

  return (
    <section className="relative py-16">
      <div className="mx-auto max-w-5xl px-5">
        <Reveal>
          <div className="relative overflow-hidden rounded-3xl border border-neon-cyan/25 bg-gradient-to-br from-neon-cyan/10 via-ink-900/60 to-neon-violet/10 p-8 md:p-12">
            <div className="pointer-events-none absolute -right-16 -top-16 h-56 w-56 rounded-full bg-neon-cyan/15 blur-3xl" />
            <div className="pointer-events-none absolute -bottom-16 -left-16 h-56 w-56 rounded-full bg-neon-violet/15 blur-3xl" />

            <div className="relative flex flex-col items-center gap-8 md:flex-row md:justify-between">
              <div className="text-center md:text-left">
                <span className="inline-flex items-center gap-1.5 rounded-full border border-neon-cyan/30 bg-neon-cyan/10 px-3 py-1 text-xs font-medium text-neon-cyan">
                  {c.badge}
                </span>
                <h2 className="mt-4 text-2xl font-bold text-white md:text-3xl">{c.title}</h2>
                <p className="mx-auto mt-3 max-w-xl text-slate-300 md:mx-0">{c.subtitle}</p>
                <ul className="mt-5 flex flex-wrap justify-center gap-x-5 gap-y-2 md:justify-start">
                  {c.perks.map((p) => (
                    <li key={p} className="flex items-center gap-1.5 text-sm text-slate-300">
                      <Check className="h-4 w-4 shrink-0 text-neon-cyan" />
                      {p}
                    </li>
                  ))}
                </ul>
              </div>

              <div className="flex shrink-0 flex-col gap-3 sm:flex-row">
                <motion.a
                  whileHover={{ scale: 1.04 }}
                  whileTap={{ scale: 0.97 }}
                  href={CHANNEL_URL}
                  target="_blank"
                  rel="noreferrer"
                  onClick={() => track("cta_click", { where: "community_channel" })}
                  className="inline-flex items-center justify-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-7 py-3.5 text-sm font-semibold text-ink-950"
                >
                  <Send className="h-4 w-4" />
                  {c.cta}
                </motion.a>
                <motion.a
                  whileHover={{ scale: 1.04 }}
                  whileTap={{ scale: 0.97 }}
                  href={GROUP_URL}
                  target="_blank"
                  rel="noreferrer"
                  onClick={() => track("cta_click", { where: "community_group" })}
                  className="inline-flex items-center justify-center gap-2 rounded-full border border-neon-cyan/40 bg-neon-cyan/10 px-7 py-3.5 text-sm font-semibold text-neon-cyan"
                >
                  <Users className="h-4 w-4" />
                  {c.groupCta}
                </motion.a>
              </div>
            </div>
          </div>
        </Reveal>
      </div>
    </section>
  );
}
