"use client";

import { useEffect, useRef, useState } from "react";
import { useInView, useReducedMotion } from "framer-motion";

interface CountUpProps {
  value: string;
  suffix?: string;
  duration?: number;
  className?: string;
}

export default function CountUp({ value, suffix = "", duration = 1.6, className }: CountUpProps) {
  const target = parseFloat(value.replace(/[^0-9.]/g, "")) || 0;
  const ref = useRef<HTMLSpanElement>(null);
  const inView = useInView(ref, { once: true, margin: "-40px" });
  const reduced = useReducedMotion();
  const [display, setDisplay] = useState(reduced ? target : 0);

  useEffect(() => {
    if (!inView || reduced) {
      setDisplay(target);
      return;
    }
    let raf = 0;
    const start = performance.now();
    const tick = (now: number) => {
      const p = Math.min((now - start) / (duration * 1000), 1);
      const eased = 1 - Math.pow(1 - p, 3);
      setDisplay(target * eased);
      if (p < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [inView, reduced, target, duration]);

  const rounded = target % 1 === 0 ? Math.round(display) : display.toFixed(1);

  return (
    <span ref={ref} className={className}>
      {rounded}
      {suffix}
    </span>
  );
}
