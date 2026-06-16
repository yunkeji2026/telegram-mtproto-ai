"use client";

import Image from "next/image";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { useEffect, useState } from "react";
import { Mic, Video, PhoneOff } from "lucide-react";
import { useLang } from "./LanguageContext";
import { useInView } from "@/lib/useInView";

function fmt(s: number) {
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
}

export default function LiveSwapStage() {
  const { t } = useLang();
  const reduced = useReducedMotion();
  const { ref, inView } = useInView<HTMLDivElement>();
  const [swapped, setSwapped] = useState(true);
  const [scanKey, setScanKey] = useState(0);
  const [sec, setSec] = useState(12);

  useEffect(() => {
    if (!inView) return;
    const clock = setInterval(() => setSec((s) => s + 1), 1000);
    return () => clearInterval(clock);
  }, [inView]);

  useEffect(() => {
    if (reduced || !inView) return;
    let flip: ReturnType<typeof setTimeout>;
    const id = setInterval(() => {
      setScanKey((k) => k + 1);
      flip = setTimeout(() => setSwapped((s) => !s), 480);
    }, 3600);
    return () => {
      clearInterval(id);
      clearTimeout(flip);
    };
  }, [reduced, inView]);

  return (
    <div ref={ref} className="relative mx-auto flex h-full w-full max-w-[460px] flex-col">
      <div className="pointer-events-none absolute -inset-6 -z-10 rounded-[2rem] bg-gradient-to-br from-neon-cyan/20 via-transparent to-neon-violet/20 blur-2xl" />

      <div className="flex h-full w-full flex-col overflow-hidden rounded-3xl border border-neon-cyan/30 bg-ink-950 shadow-[0_0_60px_-12px_rgba(34,211,238,0.35)]">
        {/* call top bar */}
        <div className="flex items-center justify-between bg-ink-900/80 px-4 py-2.5">
          <span className="inline-flex items-center gap-2 text-xs font-medium text-white">
            <span className="h-2 w-2 animate-pulse rounded-full bg-red-500" />
            {t.swap.callStatus}
            <span className="tabular-nums text-slate-400">{fmt(sec)}</span>
          </span>
          <span className="flex items-center gap-1.5">
            <span className="rounded border border-neon-cyan/30 bg-neon-cyan/10 px-1.5 py-0.5 text-[10px] font-semibold text-neon-cyan">
              HD
            </span>
            <span className="flex items-end gap-[2px]">
              {[5, 8, 11, 14].map((h, i) => (
                <span key={i} className="w-[3px] rounded-sm bg-neon-cyan/70" style={{ height: h }} />
              ))}
            </span>
          </span>
        </div>

        {/* main video (what they see) */}
        <div className="relative flex-1 overflow-hidden">
          <Image
            src="/showcase/live-before.png"
            alt={t.swap.before}
            fill
            loading="lazy"
            sizes="(max-width: 1024px) 100vw, 460px"
            className="object-cover"
          />
          <motion.div
            className="absolute inset-0"
            animate={{ opacity: swapped ? 1 : 0 }}
            transition={{ duration: 0.6, ease: "easeInOut" }}
          >
            <Image
              src="/showcase/live-after.png"
              alt={t.swap.after}
              fill
              sizes="(max-width: 1024px) 100vw, 460px"
              className="object-cover"
            />
          </motion.div>

          {/* face tracking box */}
          <div className="pointer-events-none absolute left-1/2 top-[24%] h-[40%] w-[44%] -translate-x-1/2 rounded-lg border border-neon-cyan/50">
            <span className="absolute -left-px -top-px h-3 w-3 border-l-2 border-t-2 border-neon-cyan" />
            <span className="absolute -right-px -top-px h-3 w-3 border-r-2 border-t-2 border-neon-cyan" />
            <span className="absolute -bottom-px -left-px h-3 w-3 border-b-2 border-l-2 border-neon-cyan" />
            <span className="absolute -bottom-px -right-px h-3 w-3 border-b-2 border-r-2 border-neon-cyan" />
          </div>

          {/* scan sweep */}
          {!reduced && (
            <AnimatePresence>
              <motion.div
                key={scanKey}
                initial={{ top: "-15%", opacity: 0 }}
                animate={{ top: "115%", opacity: [0, 1, 1, 0] }}
                transition={{ duration: 1.1, ease: "easeInOut" }}
                className="pointer-events-none absolute left-0 right-0 h-20"
              >
                <div className="h-full w-full bg-gradient-to-b from-transparent via-neon-cyan/25 to-transparent" />
                <div className="absolute bottom-0 left-0 right-0 h-px bg-neon-cyan shadow-[0_0_10px_rgba(34,211,238,0.9)]" />
              </motion.div>
            </AnimatePresence>
          )}

          <div className="hud-scanlines pointer-events-none absolute inset-0 opacity-30" />

          {/* "they see: face + voice" chip */}
          <span className="absolute left-3 top-3 inline-flex items-center gap-1.5 rounded-full border border-neon-cyan/50 bg-black/50 px-2.5 py-1 text-[11px] font-semibold text-neon-cyan backdrop-blur">
            {t.swap.theySee} · {t.swap.faceVoice}
          </span>

          {/* voice cloning indicator */}
          <span className="absolute right-3 top-3 inline-flex items-center gap-1.5 rounded-full border border-neon-violet/40 bg-black/50 px-2.5 py-1 text-[11px] font-medium text-neon-violet backdrop-blur">
            <Mic className="h-3 w-3" />
            {t.swap.voiceCloning}
            <span className="flex items-end gap-[2px]">
              {[6, 10, 7, 11].map((h, i) => (
                <motion.span
                  key={i}
                  className="w-[2px] rounded-full bg-neon-violet"
                  style={{ height: h }}
                  animate={reduced ? undefined : { scaleY: [0.4, 1, 0.5, 1] }}
                  transition={{ duration: 1, repeat: Infinity, delay: i * 0.1 }}
                />
              ))}
            </span>
          </span>

          {/* PiP: your real self */}
          <div className="absolute bottom-3 right-3 w-[32%] overflow-hidden rounded-xl border border-white/25 shadow-lg">
            <div className="relative aspect-[3/4]">
              <Image
                src="/showcase/live-before.png"
                alt={t.swap.you}
                fill
                sizes="150px"
                className="object-cover"
              />
              <span className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/70 to-transparent px-1.5 py-1 text-center text-[9px] font-medium text-white">
                {t.swap.you}
              </span>
            </div>
          </div>

          {/* corner brackets */}
          <span className="pointer-events-none absolute left-2 top-2 h-4 w-4 border-l-2 border-t-2 border-neon-cyan/40" />
          <span className="pointer-events-none absolute right-2 top-2 h-4 w-4 border-r-2 border-t-2 border-neon-cyan/40" />
        </div>

        {/* call control bar */}
        <div className="flex items-center justify-center gap-4 bg-ink-900/80 py-3">
          <span className="relative grid h-10 w-10 place-items-center rounded-full bg-white/10 text-white">
            <Mic className="h-4 w-4" />
            <span className="absolute inset-0 animate-ping rounded-full border border-neon-violet/40" />
          </span>
          <span className="grid h-10 w-10 place-items-center rounded-full bg-white/10 text-white">
            <Video className="h-4 w-4" />
          </span>
          <span className="grid h-10 w-12 place-items-center rounded-full bg-red-500 text-white">
            <PhoneOff className="h-4 w-4" />
          </span>
          <span className="ml-1 hidden items-center text-[10px] tabular-nums text-slate-500 sm:inline-flex">
            {t.swap.hudFps} · {t.swap.hudLatency}
          </span>
        </div>
      </div>
    </div>
  );
}
