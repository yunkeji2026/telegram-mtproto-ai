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

/** 拥有 zh/en 双路由的营销页根路径（"" = 首页）。落地页/条款页切语言时走 URL 前缀互换，
 *  保证分享链接与 SEO 语言一致；其余路由（/admin /app 等）仅切换字典。 */
const DUAL_LOCALE_BASES = new Set(["", "/voice", "/face", "/interpreting", "/asset-safe", "/nurture", "/download", "/privacy", "/terms"]);

function dualLocaleBase(pathname: string | null): string | null {
  if (!pathname) return null;
  const base = pathname === "/en" ? "" : pathname.startsWith("/en/") ? pathname.slice(3) : pathname === "/" ? "" : pathname;
  return DUAL_LOCALE_BASES.has(base) ? base : null;
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
    // On dual-locale marketing routes, reflect locale in the URL (shareable + crawlable).
    const base = dualLocaleBase(pathname);
    if (base !== null) {
      setLocal("hl-lang", next);
      router.push(next === "en" ? `/en${base}` || "/en" : base || "/");
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
