"use client";

import Image from "next/image";
import { motion, useReducedMotion } from "framer-motion";
import { useLang } from "./LanguageContext";

export default function DigitalHumanCard() {
  const { t } = useLang();
  const d = t.digitalDemo;
  const reduced = useReducedMotion();

  return (
    <div className="relative mx-auto w-full max-w-[420px]">
      <div className="pointer-events-none absolute -inset-4 -z-10 rounded-3xl bg-gradient-to-br from-neon-violet/20 to-neon-cyan/15 blur-2xl" />
      <div className="relative aspect-[4/3] overflow-hidden rounded-2xl border border-neon-violet/30 bg-ink-900 shadow-2xl">
        <Image
          src="/showcase/digital-human.png"
          alt={d.title}
          fill
          sizes="(max-width: 1024px) 100vw, 420px"
          className="object-cover"
        />
        <div className="absolute inset-0 bg-gradient-to-t from-ink-950/80 via-transparent to-transparent" />

        {/* badge */}
        <span className="absolute left-3 top-3 inline-flex items-center gap-1.5 rounded-full border border-neon-violet/40 bg-black/40 px-2.5 py-1 text-[11px] font-medium text-neon-violet backdrop-blur">
          <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-neon-violet" />
          {d.badge}
        </span>

        {/* talking equalizer (lip-sync hint) */}
        <div className="absolute bottom-3 left-3 flex items-end gap-[3px] rounded-full bg-black/45 px-2.5 py-2 backdrop-blur">
          {[10, 16, 8, 18, 12, 20, 9].map((h, i) => (
            <motion.span
              key={i}
              className="w-[3px] rounded-full bg-neon-cyan"
              style={{ height: h }}
              animate={reduced ? undefined : { scaleY: [0.4, 1, 0.5, 0.9, 0.4] }}
              transition={{ duration: 1, repeat: Infinity, delay: i * 0.08 }}
            />
          ))}
        </div>

        {/* tags */}
        <div className="absolute bottom-3 right-3 flex max-w-[60%] flex-wrap justify-end gap-1.5">
          {d.tags.map((tag) => (
            <span
              key={tag}
              className="rounded-full border border-white/15 bg-black/45 px-2 py-0.5 text-[10px] text-slate-200 backdrop-blur"
            >
              {tag}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}
