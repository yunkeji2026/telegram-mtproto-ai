"use client";

import { useEffect, useState } from "react";
import { Menu, X, Languages, Download } from "lucide-react";
import { useLang } from "./LanguageContext";
import { useTelegram } from "./TelegramProvider";
import { CONTACT_URL } from "@/lib/site";
import { track } from "@/lib/track";
import BrandMark from "./BrandMark";
import { BRAND } from "@/lib/brand";

export default function Navbar() {
  const { t, lang, toggle } = useLang();
  const { isMiniApp } = useTelegram();
  const [scrolled, setScrolled] = useState(false);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState("");

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 20);
    onScroll();
    window.addEventListener("scroll", onScroll);
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  useEffect(() => {
    const ids = ["autochat", "realtime", "showcase", "engage", "pricing", "contact"];
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((e) => {
          if (e.isIntersecting) setActive(e.target.id);
        });
      },
      { rootMargin: "-45% 0px -50% 0px" }
    );
    ids.forEach((id) => {
      const el = document.getElementById(id);
      if (el) observer.observe(el);
    });
    return () => observer.disconnect();
  }, []);

  const links = [
    { href: "#autochat", label: t.nav.autochat },
    { href: "#realtime", label: t.nav.demo },
    { href: "#showcase", label: t.nav.solutions },
    { href: "#engage", label: t.nav.engage },
    { href: "#pricing", label: t.nav.pricing },
    { href: "#contact", label: t.nav.contact },
  ];

  // 下载页是独立路由（非首页锚点），按当前语言走 zh/en 前缀。
  const downloadHref = lang === "zh" ? "/download" : "/en/download";
  const downloadLabel = lang === "zh" ? "下载" : "Download";

  return (
    <header
      className={`fixed inset-x-0 top-0 z-50 transition-all ${
        scrolled ? "glass" : "bg-transparent"
      }`}
    >
      <nav className="mx-auto flex max-w-7xl items-center justify-between px-5 py-4">
        <a href="#top" className="flex items-center gap-2">
          <BrandMark className="h-9 w-9" />
          <span className="text-lg font-semibold tracking-wide text-white">
            {BRAND.company.zh} <span className="text-slate-400">{BRAND.company.en}</span>
          </span>
        </a>

        <div className="hidden items-center gap-8 md:flex">
          {links.map((l) => (
            <a
              key={l.href}
              href={l.href}
              className={`relative text-sm transition-colors hover:text-white ${
                active === l.href.slice(1) ? "text-white" : "text-slate-300"
              }`}
            >
              {l.label}
              {active === l.href.slice(1) && (
                <span className="absolute -bottom-1.5 left-0 h-0.5 w-full rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet" />
              )}
            </a>
          ))}
          <a href="/brand" className="relative text-sm text-slate-300 transition-colors hover:text-white">
            {lang === "zh" ? "品牌" : "Brand"}
          </a>
        </div>

        <div className="flex items-center gap-3">
          <a
            href={downloadHref}
            onClick={() => track("cta_click", { where: "nav_download" })}
            className="hidden items-center gap-1.5 rounded-full border border-neon-cyan/40 px-3.5 py-1.5 text-xs font-medium text-neon-cyan transition hover:bg-neon-cyan/10 sm:inline-flex"
          >
            <Download className="h-4 w-4" />
            {downloadLabel}
          </a>
          <button
            onClick={toggle}
            className="flex items-center gap-1.5 rounded-full border border-white/10 px-3 py-1.5 text-xs text-slate-300 transition hover:border-neon-cyan/50 hover:text-white"
            aria-label="switch language"
          >
            <Languages className="h-4 w-4" />
            {lang === "zh" ? "EN" : "中文"}
          </button>
          {isMiniApp ? (
            <a
              href="#contact"
              onClick={() => track("cta_click", { where: "nav_miniapp" })}
              className="hidden rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-4 py-2 text-sm font-medium text-ink-950 transition hover:opacity-90 md:inline-block"
            >
              {t.nav.cta}
            </a>
          ) : (
            <a
              href={CONTACT_URL}
              target="_blank"
              rel="noreferrer"
              onClick={() => track("cta_click", { where: "nav" })}
              className="hidden rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-4 py-2 text-sm font-medium text-ink-950 transition hover:opacity-90 md:inline-block"
            >
              {t.nav.cta}
            </a>
          )}
          <button
            className="text-slate-200 md:hidden"
            onClick={() => setOpen((v) => !v)}
            aria-label="menu"
          >
            {open ? <X className="h-6 w-6" /> : <Menu className="h-6 w-6" />}
          </button>
        </div>
      </nav>

      {open && (
        <div className="glass border-t border-white/5 md:hidden">
          <div className="flex flex-col gap-1 px-5 py-3">
            {links.map((l) => (
              <a
                key={l.href}
                href={l.href}
                onClick={() => setOpen(false)}
                className="rounded-lg px-3 py-2 text-sm text-slate-300 hover:bg-white/5 hover:text-white"
              >
                {l.label}
              </a>
            ))}
            <a
              href="/brand"
              onClick={() => setOpen(false)}
              className="rounded-lg px-3 py-2 text-sm text-slate-300 hover:bg-white/5 hover:text-white"
            >
              {lang === "zh" ? "品牌" : "Brand"}
            </a>
            <a
              href={downloadHref}
              onClick={() => { setOpen(false); track("cta_click", { where: "nav_download_mobile" }); }}
              className="flex items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium text-neon-cyan hover:bg-neon-cyan/10"
            >
              <Download className="h-4 w-4" />
              {downloadLabel}
            </a>
            <a
              href={isMiniApp ? "#contact" : CONTACT_URL}
              target={isMiniApp ? undefined : "_blank"}
              rel={isMiniApp ? undefined : "noreferrer"}
              className="mt-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-4 py-2 text-center text-sm font-medium text-ink-950"
            >
              {t.nav.cta}
            </a>
          </div>
        </div>
      )}
    </header>
  );
}
