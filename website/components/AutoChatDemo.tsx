"use client";

import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { useEffect, useState } from "react";
import { Languages, Bot, Inbox, Play, Sparkles } from "lucide-react";
import { useLang } from "./LanguageContext";
import { useInView } from "@/lib/useInView";

const PLATFORMS = [
  { label: "TG", color: "#229ED9" },
  { label: "LINE", color: "#06C755" },
  { label: "WA", color: "#25D366" },
  { label: "MSG", color: "#0084FF" },
];

const VOICE_BARS = [6, 11, 8, 16, 10, 18, 9, 14, 7, 13, 17, 9, 12, 6, 15, 8];

export default function AutoChatDemo() {
  const { t } = useLang();
  const d = t.autochat.demo;
  const reduced = useReducedMotion();
  const { ref, inView } = useInView<HTMLDivElement>();
  const [step, setStep] = useState(reduced ? 5 : 0);

  useEffect(() => {
    if (reduced || !inView) return;
    const id = setInterval(() => setStep((s) => (s + 1) % 6), 1600);
    return () => clearInterval(id);
  }, [reduced, inView]);

  return (
    <div ref={ref} className="relative mx-auto w-full max-w-[420px]">
      <div className="pointer-events-none absolute -inset-4 -z-10 rounded-3xl bg-gradient-to-br from-neon-cyan/20 to-neon-violet/20 blur-2xl" />
      <div className="overflow-hidden rounded-2xl border border-white/10 bg-ink-900/90 shadow-2xl">
        {/* header */}
        <div className="flex items-center justify-between border-b border-white/10 bg-ink-800/60 px-4 py-3">
          <span className="flex items-center gap-2 text-sm font-medium text-white">
            <Inbox className="h-4 w-4 text-neon-cyan" />
            {d.inbox}
          </span>
          <span className="flex gap-1">
            {PLATFORMS.map((p) => (
              <span
                key={p.label}
                className="grid h-5 w-7 place-items-center rounded text-[8px] font-bold text-white"
                style={{ background: p.color }}
              >
                {p.label}
              </span>
            ))}
          </span>
        </div>

        {/* body */}
        <div className="flex min-h-[360px] flex-col gap-3 p-4">
          {/* incoming foreign message */}
          <AnimatePresence>
            {step >= 1 && (
              <Bubble key="in" side="in" name={`${d.incoming.flag} ${d.incoming.name}`}>
                <p className="text-sm text-slate-100">{d.incoming.text}</p>
                <Translated tag={d.translatedTag}>{d.incoming.translated}</Translated>
              </Bubble>
            )}
          </AnimatePresence>

          {/* typing */}
          <AnimatePresence>
            {step === 2 && (
              <motion.div
                initial={{ opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0 }}
                className="ml-auto flex items-center gap-1.5 rounded-full bg-neon-cyan/15 px-3 py-2"
              >
                <Bot className="h-3.5 w-3.5 text-neon-cyan" />
                <span className="flex gap-1">
                  {[0, 1, 2].map((i) => (
                    <motion.span
                      key={i}
                      className="h-1.5 w-1.5 rounded-full bg-neon-cyan"
                      animate={{ opacity: [0.3, 1, 0.3] }}
                      transition={{ duration: 0.9, repeat: Infinity, delay: i * 0.18 }}
                    />
                  ))}
                </span>
              </motion.div>
            )}
          </AnimatePresence>

          {/* AI auto-close reply */}
          <AnimatePresence>
            {step >= 3 && (
              <Bubble key="reply" side="out" name={d.personaName} ai tag={d.autoTag}>
                <p className="text-sm text-white">{d.reply.text}</p>
                <p className="mt-1 text-[11px] text-ink-950/70">{d.reply.translated}</p>
              </Bubble>
            )}
          </AnimatePresence>

          {/* persona voice message */}
          <AnimatePresence>
            {step >= 4 && (
              <motion.div
                layout
                initial={{ opacity: 0, y: 10, scale: 0.96 }}
                animate={{ opacity: 1, y: 0, scale: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.3 }}
                className="ml-auto max-w-[85%]"
              >
                <span className="mb-1 block text-right text-[11px] text-neon-violet">
                  <Sparkles className="mr-1 inline h-3 w-3" />
                  {d.voiceTag}
                </span>
                <div className="flex items-center gap-2.5 rounded-2xl rounded-tr-sm border border-neon-violet/30 bg-gradient-to-r from-neon-violet/20 to-neon-cyan/15 px-3 py-2.5">
                  <span className="grid h-7 w-7 shrink-0 place-items-center rounded-full bg-neon-violet/80 text-ink-950">
                    <Play className="ml-0.5 h-3.5 w-3.5" />
                  </span>
                  <span className="flex h-6 flex-1 items-center gap-[2px]">
                    {VOICE_BARS.map((h, i) => (
                      <motion.span
                        key={i}
                        className="w-full rounded-full bg-neon-violet/80"
                        style={{ height: `${h * 3}px`, maxHeight: "100%" }}
                        animate={reduced ? undefined : { scaleY: [0.5, 1, 0.6, 1] }}
                        transition={{ duration: 1.1, repeat: Infinity, delay: i * 0.05 }}
                      />
                    ))}
                  </span>
                  <span className="shrink-0 text-[10px] tabular-nums text-slate-300">{d.voiceLen}</span>
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      </div>
    </div>
  );
}

function Bubble({
  side,
  name,
  ai,
  tag,
  children,
}: {
  side: "in" | "out";
  name: string;
  ai?: boolean;
  tag?: string;
  children: React.ReactNode;
}) {
  const out = side === "out";
  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 10, scale: 0.96 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.3 }}
      className={`max-w-[85%] ${out ? "ml-auto" : "mr-auto"}`}
    >
      <span className={`mb-1 flex items-center gap-1.5 text-[11px] ${out ? "justify-end text-neon-cyan" : "text-slate-400"}`}>
        {ai && "🤖 "}
        {name}
        {tag && (
          <span className="rounded-full bg-neon-cyan/20 px-1.5 py-0.5 text-[9px] font-semibold text-neon-cyan">
            {tag}
          </span>
        )}
      </span>
      <div
        className={`rounded-2xl px-3.5 py-2.5 ${
          out
            ? "rounded-tr-sm bg-gradient-to-r from-neon-cyan to-neon-violet text-ink-950"
            : "rounded-tl-sm border border-white/10 bg-ink-800/80"
        }`}
      >
        {children}
      </div>
    </motion.div>
  );
}

function Translated({ tag, children }: { tag: string; children: React.ReactNode }) {
  return (
    <div className="mt-2 flex items-start gap-1.5 border-t border-white/10 pt-2">
      <span className="mt-0.5 inline-flex shrink-0 items-center gap-1 rounded bg-neon-cyan/15 px-1.5 py-0.5 text-[10px] font-medium text-neon-cyan">
        <Languages className="h-3 w-3" />
        {tag}
      </span>
      <p className="text-sm text-neon-cyan/90">{children}</p>
    </div>
  );
}
