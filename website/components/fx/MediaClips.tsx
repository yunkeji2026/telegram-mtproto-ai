"use client";

import { useRef, useState } from "react";
import Image from "next/image";
import { Play, Pause, PlayCircle } from "lucide-react";

/** 首页 RealProof 与产品落地页共用的真实样片播放器（音频条 + 竖版视频卡）。 */

export function AudioClip({ label, src }: { label: string; src: string }) {
  const ref = useRef<HTMLAudioElement>(null);
  const [ok, setOk] = useState(true);
  const [playing, setPlaying] = useState(false);
  const [progress, setProgress] = useState(0);

  const toggle = () => {
    const el = ref.current;
    if (!el || !ok) return;
    if (playing) {
      el.pause();
    } else {
      void el.play().catch(() => setOk(false));
    }
  };

  return (
    <div
      className={`flex items-center gap-3 rounded-xl border p-3 ${
        ok ? "border-white/10 bg-ink-800/50" : "border-white/5 bg-ink-800/30 opacity-60"
      }`}
    >
      <button
        onClick={toggle}
        disabled={!ok}
        aria-label={label}
        className={`grid h-9 w-9 shrink-0 place-items-center rounded-full transition ${
          ok ? "bg-gradient-to-br from-neon-cyan to-neon-violet text-ink-950" : "bg-white/10 text-slate-500"
        }`}
      >
        {playing ? <Pause className="h-4 w-4" /> : <Play className="ml-0.5 h-4 w-4" />}
      </button>
      <div className="min-w-0 flex-1">
        <div className="mb-1 text-xs font-medium text-slate-200">{label}</div>
        <div className="h-1.5 w-full overflow-hidden rounded-full bg-white/10">
          <div
            className="h-full rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet transition-[width] duration-150"
            style={{ width: `${ok ? progress * 100 : 0}%` }}
          />
        </div>
      </div>
      <audio
        ref={ref}
        src={src}
        preload="none"
        onError={() => setOk(false)}
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onEnded={() => {
          setPlaying(false);
          setProgress(0);
        }}
        onTimeUpdate={(e) => {
          const el = e.currentTarget;
          if (el.duration) setProgress(el.currentTime / el.duration);
        }}
      />
    </div>
  );
}

export function VideoClip({ src, poster, pending }: { src: string; poster: string; pending: string }) {
  const [ok, setOk] = useState(true);
  return (
    <div className="relative mx-auto aspect-[11/16] w-full max-w-[300px] overflow-hidden rounded-2xl border border-neon-violet/25 bg-ink-900">
      {ok ? (
        <video
          className="h-full w-full object-cover"
          src={src}
          poster={poster}
          controls
          playsInline
          preload="none"
          onError={() => setOk(false)}
        />
      ) : (
        <>
          <Image src={poster} alt="digital human" fill sizes="300px" className="object-cover opacity-70" />
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-ink-950/55 p-4 text-center backdrop-blur-[2px]">
            <PlayCircle className="h-10 w-10 text-neon-cyan/80" />
            <p className="max-w-xs text-xs leading-relaxed text-slate-200">{pending}</p>
          </div>
        </>
      )}
    </div>
  );
}
