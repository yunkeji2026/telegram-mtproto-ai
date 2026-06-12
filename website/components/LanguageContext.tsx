"use client";

import { createContext, useContext, useEffect, useState, ReactNode } from "react";
import { content, Dict, Lang } from "@/lib/content";
import { getLocal, setLocal } from "@/lib/safe-storage";

interface LanguageContextValue {
  lang: Lang;
  setLang: (lang: Lang) => void;
  toggle: () => void;
  t: Dict;
}

const LanguageContext = createContext<LanguageContextValue | null>(null);

export function LanguageProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>("zh");

  useEffect(() => {
    // hl-lang is the current key; fall back to legacy yt-lang once for returning users.
    const saved = (getLocal("hl-lang") ?? getLocal("yt-lang")) as Lang | null;
    if (saved === "zh" || saved === "en") {
      setLangState(saved);
    } else if (typeof navigator !== "undefined" && navigator.language.startsWith("en")) {
      setLangState("en");
    }
  }, []);

  const setLang = (next: Lang) => {
    setLangState(next);
    setLocal("hl-lang", next);
    if (typeof document !== "undefined") document.documentElement.lang = next === "zh" ? "zh-CN" : "en";
  };

  const toggle = () => setLang(lang === "zh" ? "en" : "zh");

  return (
    <LanguageContext.Provider value={{ lang, setLang, toggle, t: content[lang] }}>
      {children}
    </LanguageContext.Provider>
  );
}

export function useLang() {
  const ctx = useContext(LanguageContext);
  if (!ctx) throw new Error("useLang must be used within LanguageProvider");
  return ctx;
}
