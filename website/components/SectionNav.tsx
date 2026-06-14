"use client";

import { useEffect, useState } from "react";
import { useLang } from "./LanguageContext";

export default function SectionNav() {
  const { t, lang } = useLang();
  const [active, setActive] = useState("top");

  const items = [
    { id: "top", label: lang === "zh" ? "首页" : "Home" },
    { id: "products", label: lang === "zh" ? "产品" : "Products" },
    { id: "autochat", label: t.nav.autochat },
    { id: "realtime", label: t.nav.demo },
    { id: "showcase", label: t.nav.solutions },
    { id: "cases", label: t.nav.cases },
    { id: "engage", label: t.nav.engage },
    { id: "pricing", label: t.nav.pricing },
    { id: "contact", label: t.nav.contact },
  ];

  useEffect(() => {
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((e) => e.isIntersecting && setActive(e.target.id));
      },
      { rootMargin: "-45% 0px -50% 0px" }
    );
    items.forEach((it) => {
      const el = document.getElementById(it.id);
      if (el) observer.observe(el);
    });
    return () => observer.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <nav className="fixed right-5 top-1/2 z-40 hidden -translate-y-1/2 flex-col gap-3 lg:flex">
      {items.map((it) => {
        const on = active === it.id;
        return (
          <a key={it.id} href={`#${it.id}`} className="group flex items-center justify-end gap-2">
            <span
              className={`pointer-events-none rounded-md bg-ink-800/90 px-2 py-1 text-[11px] text-slate-200 opacity-0 transition group-hover:opacity-100 ${
                on ? "opacity-100" : ""
              }`}
            >
              {it.label}
            </span>
            <span
              className={`h-2.5 w-2.5 rounded-full border transition-all ${
                on
                  ? "scale-125 border-transparent bg-gradient-to-r from-neon-cyan to-neon-violet"
                  : "border-white/30 bg-transparent group-hover:border-neon-cyan"
              }`}
            />
          </a>
        );
      })}
    </nav>
  );
}
