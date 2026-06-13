"use client";

import { createContext, useContext, useEffect, useState, ReactNode } from "react";
import { usePathname, useRouter } from "next/navigation";
import { content, Dict, Lang } from "@/lib/content";
import { getLocal, setLocal } from "@/lib/safe-storage";

interface LanguageContextValue {
  lang: Lang;
  setLang: (lang: Lang) => void;
  toggle: () => void;
  t: Dict;
}

const LanguageContext = createContext<LanguageContextValue | null>(null);

/** The `/en` route forces English so the SSR'd HTML is independently indexable. */
function routeLangOf(pathname: string | null): Lang | null {
  if (!pathname) return null;
  return pathname === "/en" || pathname.startsWith("/en/") ? "en" : null;
}

function isHome(pathname: string | null): boolean {
  return pathname === "/" || pathname === "/en" || (pathname?.startsWith("/en/") ?? false);
}

export function LanguageProvider({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const routeLang = routeLangOf(pathname);
  // SSR + first client render both honor the route locale -> no hydration mismatch on /en.
  const [lang, setLangState] = useState<Lang>(routeLang ?? "zh");

  useEffect(() => {
    if (routeLang) {
      setLangState(routeLang);
      if (typeof document !== "undefined") document.documentElement.lang = routeLang === "zh" ? "zh-CN" : "en";
      return;
    }
    // Non-/en routes: honor saved preference (hl-lang, legacy yt-lang) then browser language.
    const saved = (getLocal("hl-lang") ?? getLocal("yt-lang")) as Lang | null;
    if (saved === "zh" || saved === "en") {
      setLangState(saved);
    } else if (typeof navigator !== "undefined" && navigator.language.startsWith("en")) {
      setLangState("en");
    }
  }, [routeLang]);

  const setLang = (next: Lang) => {
    setLangState(next);
    setLocal("hl-lang", next);
    if (typeof document !== "undefined") document.documentElement.lang = next === "zh" ? "zh-CN" : "en";
  };

  const toggle = () => {
    const next: Lang = lang === "zh" ? "en" : "zh";
    // On the marketing homepage, reflect locale in the URL (shareable + crawlable).
    if (isHome(pathname)) {
      setLocal("hl-lang", next);
      router.push(next === "en" ? "/en" : "/");
      return;
    }
    setLang(next);
  };

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
