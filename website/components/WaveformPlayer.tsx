"use client";

import { useEffect, useRef, useState } from "react";
import { Play, Pause, Mic, Sparkles } from "lucide-react";
import { useLang } from "./LanguageContext";

const BARS = [
  8, 14, 22, 16, 30, 24, 38, 28, 44, 34, 26, 40, 20, 32, 46, 36, 24, 18, 30, 42,
  34, 22, 14, 28, 38, 30, 20, 26, 16, 34, 44, 28, 18, 24, 36, 22, 12, 20, 30, 16,
];

function Track({
  label,
  icon,
  accent,
}: {
  label: string;
  icon: React.ReactNode;
  accent: boolean;
}) {
  const [playing, setPlaying] = useState(false);
  const [progress, setProgress] = useState(0);
  const raf = useRef<number>();

  useEffect(() => {
    if (!playing) return;
    const start = performance.now();
    const dur = 3600;
    const tick = (now: number) => {
      const p = Math.min(1, (now - start) / dur);
      setProgress(p);
      if (p < 1) raf.current = requestAnimationFrame(tick);
      else setPlaying(false);
    };
    raf.current = requestAnimationFrame(tick);
    return () => {
      if (raf.current) cancelAnimationFrame(raf.current);
    };
  }, [playing]);

  return (
    <div
      className={`flex items-center gap-3 rounded-2xl border p-3 ${
        accent
          ? "border-neon-cyan/30 bg-gradient-to-r from-neon-cyan/10 to-neon-violet/10"
          : "border-white/10 bg-ink-800/50"
      }`}
    >
      <button
        onClick={() => {
          setProgress(0);
          setPlaying((p) => !p);
        }}
        aria-label={label}
        className={`grid h-10 w-10 shrink-0 place-items-center rounded-full transition ${
          accent
            ? "bg-gradient-to-br from-neon-cyan to-neon-violet text-ink-950"
            : "bg-white/10 text-white hover:bg-white/20"
        }`}
      >
        {playing ? <Pause className="h-4 w-4" /> : <Play className="ml-0.5 h-4 w-4" />}
      </button>

      <div className="min-w-0 flex-1">
        <div className="mb-1.5 flex items-center gap-1.5 text-xs text-slate-300">
          {icon}
          {label}
        </div>
        <div className="flex h-8 items-center gap-[2px]">
          {BARS.map((h, i) => {
            const reached = playing && i / BARS.length <= progress;
            return (
              <span
                key={i}
                className="w-full rounded-full transition-colors"
                style={{
                  height: `${h}%`,
                  minHeight: 3,
                  background: reached
                    ? accent
                      ? "#22d3ee"
                      : "#e2e8f0"
                    : accent
                    ? "rgba(34,211,238,0.28)"
                    : "rgba(255,255,255,0.18)",
                }}
              />
            );
          })}
        </div>
      </div>
    </div>
  );
}

export default function WaveformPlayer() {
  const { t } = useLang();
  const v = t.voiceDemo;
  return (
    <div className="relative mx-auto w-full max-w-[440px]">
      <div className="pointer-events-none absolute -inset-4 -z-10 rounded-3xl bg-gradient-to-br from-neon-violet/15 to-neon-cyan/15 blur-2xl" />
      <div className="space-y-3 rounded-2xl border border-white/10 bg-ink-900/80 p-4 shadow-2xl">
        <Track label={v.original} icon={<Mic className="h-3.5 w-3.5 text-slate-400" />} accent={false} />
        <div className="flex justify-center">
          <span className="rounded-full border border-neon-cyan/30 bg-neon-cyan/10 px-3 py-1 text-[11px] font-medium text-neon-cyan">
            ↓ AI Clone
          </span>
        </div>
        <Track label={v.cloned} icon={<Sparkles className="h-3.5 w-3.5 text-neon-cyan" />} accent />

        <div className="border-t border-white/10 pt-3">
          <p className="mb-2 text-[11px] text-slate-400">{v.langsLabel}</p>
          <div className="flex flex-wrap gap-1.5">
            {v.langs.map((l) => (
              <span
                key={l}
                className="rounded-full border border-white/10 bg-white/5 px-2.5 py-1 text-[11px] text-slate-300"
              >
                {l}
              </span>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
