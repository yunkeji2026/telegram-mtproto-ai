"use client";

import Image from "next/image";
import { useRef, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";

export default function BeforeAfter({
  before,
  after,
  beforeLabel,
  afterLabel,
  hint,
  priority = false,
}: {
  before: string;
  after: string;
  beforeLabel: string;
  afterLabel: string;
  hint?: string;
  priority?: boolean;
}) {
  const [pos, setPos] = useState(52);
  const ref = useRef<HTMLDivElement>(null);
  const dragging = useRef(false);

  const setFromClientX = (clientX: number) => {
    const el = ref.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const p = ((clientX - rect.left) / rect.width) * 100;
    setPos(Math.max(3, Math.min(97, p)));
  };

  return (
    <div
      ref={ref}
      className="group relative aspect-[4/3] w-full cursor-ew-resize touch-none select-none overflow-hidden rounded-2xl border border-neon-cyan/25 bg-ink-900"
      onPointerDown={(e) => {
        dragging.current = true;
        (e.currentTarget as Element).setPointerCapture?.(e.pointerId);
        setFromClientX(e.clientX);
      }}
      onPointerMove={(e) => dragging.current && setFromClientX(e.clientX)}
      onPointerUp={() => (dragging.current = false)}
      onPointerCancel={() => (dragging.current = false)}
    >
      {/* base = after (swapped) */}
      <Image
        src={after}
        alt={afterLabel}
        fill
        priority={priority}
        sizes="(max-width: 768px) 100vw, 600px"
        className="object-cover"
      />
      {/* overlay = before (original), clipped from left */}
      <div className="absolute inset-0" style={{ clipPath: `inset(0 ${100 - pos}% 0 0)` }}>
        <Image
          src={before}
          alt={beforeLabel}
          fill
          priority={priority}
          sizes="(max-width: 768px) 100vw, 600px"
          className="object-cover"
        />
      </div>

      {/* labels */}
      <span className="pointer-events-none absolute left-3 top-3 rounded-full border border-white/15 bg-black/45 px-2.5 py-1 text-[11px] font-medium text-slate-200 backdrop-blur">
        {beforeLabel}
      </span>
      <span className="pointer-events-none absolute right-3 top-3 rounded-full border border-neon-cyan/40 bg-neon-cyan/15 px-2.5 py-1 text-[11px] font-medium text-neon-cyan backdrop-blur">
        {afterLabel}
      </span>

      {/* divider + handle */}
      <div className="pointer-events-none absolute inset-y-0" style={{ left: `${pos}%` }}>
        <div className="absolute inset-y-0 -ml-px w-[2px] bg-neon-cyan/90 shadow-[0_0_14px_rgba(34,211,238,0.85)]" />
        <div className="absolute top-1/2 flex -translate-x-1/2 -translate-y-1/2 items-center gap-0.5 rounded-full bg-white px-1.5 py-1.5 text-ink-950 shadow-lg ring-2 ring-neon-cyan/40">
          <ChevronLeft className="h-3.5 w-3.5" />
          <ChevronRight className="h-3.5 w-3.5" />
        </div>
      </div>

      {hint && (
        <span className="pointer-events-none absolute bottom-3 left-1/2 -translate-x-1/2 rounded-full border border-white/10 bg-black/45 px-3 py-1 text-[11px] text-slate-300 backdrop-blur transition-opacity group-hover:opacity-0">
          {hint}
        </span>
      )}
    </div>
  );
}
