"use client";

import { useEffect, useState } from "react";
import { Menu, X, Languages, ChevronDown } from "lucide-react";
import { useLang } from "./LanguageContext";
import { useTelegram } from "./TelegramProvider";
import { CONTACT_URL } from "@/lib/site";
import { track } from "@/lib/track";
import BrandMark from "./BrandMark";
import { BRAND, CATEGORIES, CATEGORY_ORDER, productsInCategory, type ProductKey } from "@/lib/brand";
import { PRODUCT_LANDING, PRODUCT_ANCHOR } from "./productMeta";

export default function Navbar() {
  const { t, lang, toggle } = useLang();
  const { isMiniApp } = useTelegram();
  const [scrolled, setScrolled] = useState(false);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState("");
  const [productsOpen, setProductsOpen] = useState(false);       // 桌面 mega menu
  const [mobileProductsOpen, setMobileProductsOpen] = useState(false); // 移动端折叠

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

  // 产品链接：有独立落地页跳落地页（带 /en 前缀），否则回首页锚点（带 home 前缀，
  // 保证在任意页面点击都能正确跳转——修掉现有纯 #hash 在非首页失效的问题）。
  const productHref = (key: ProductKey) => {
    const landing = PRODUCT_LANDING[key];
    if (landing) return lang === "zh" ? landing : `/en${landing}`;
    const home = lang === "zh" ? "/" : "/en";
    return `${home}${PRODUCT_ANCHOR[key]}`;
  };

  const links = [
    { href: "#autochat", label: t.nav.autochat },
    { href: "#realtime", label: t.nav.demo },
    { href: "#showcase", label: t.nav.solutions },
    { href: "#engage", label: t.nav.engage },
    { href: "#pricing", label: t.nav.pricing },
    { href: "#contact", label: t.nav.contact },
  ];

  const productsLabel = lang === "zh" ? "产品" : "Products";

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

        <div className="hidden items-center gap-7 md:flex">
          {/* 产品下拉：按三系（智连/幻境/通达）分组 */}
          <div
            className="relative"
            onMouseEnter={() => setProductsOpen(true)}
            onMouseLeave={() => setProductsOpen(false)}
          >
            <button
              className={`flex items-center gap-1 text-sm transition-colors hover:text-white ${
                productsOpen ? "text-white" : "text-slate-300"
              }`}
              aria-expanded={productsOpen}
              aria-haspopup="true"
            >
              {productsLabel}
              <ChevronDown className={`h-3.5 w-3.5 transition-transform ${productsOpen ? "rotate-180" : ""}`} />
            </button>

            {productsOpen && (
              // pt-3 桥接按钮与面板，避免鼠标移入间隙时下拉闪退
              <div className="absolute left-1/2 top-full -translate-x-1/2 pt-3">
                <div className="glass w-[min(90vw,660px)] rounded-2xl border border-white/10 p-4 shadow-2xl">
                  <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
                    {CATEGORY_ORDER.map((cat) => {
                      const cc = CATEGORIES[cat];
                      return (
                        <div key={cat}>
                          <div className="px-2 pb-1.5">
                            <div className="flex items-baseline gap-1.5">
                              <span className="text-sm font-bold text-white">{cc.zh}</span>
                              <span className="text-[11px] font-semibold uppercase tracking-wider text-neon-cyan">
                                {cc.en}
                              </span>
                            </div>
                            <div className="text-[11px] text-slate-500">{cc.tagline[lang]}</div>
                          </div>
                          <div className="flex flex-col">
                            {productsInCategory(cat).map((key) => {
                              const p = BRAND.products[key];
                              return (
                                <a
                                  key={key}
                                  href={productHref(key)}
                                  onClick={() => {
                                    track("product_click", { key, where: "nav" });
                                    setProductsOpen(false);
                                  }}
                                  className="group/item rounded-lg px-2 py-1.5 transition hover:bg-white/5"
                                >
                                  <div className="flex items-baseline gap-1.5">
                                    <span className="text-sm text-slate-100 group-hover/item:text-white">{p.zh}</span>
                                    <span className="text-[11px] text-neon-cyan">{p.en}</span>
                                  </div>
                                  <div className="line-clamp-1 text-[11px] text-slate-500">{p.desc[lang]}</div>
                                </a>
                              );
                            })}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              </div>
            )}
          </div>

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
            {/* 移动端：产品分组（可折叠） */}
            <button
              onClick={() => setMobileProductsOpen((v) => !v)}
              className="flex items-center justify-between rounded-lg px-3 py-2 text-sm text-slate-200 hover:bg-white/5"
              aria-expanded={mobileProductsOpen}
            >
              {productsLabel}
              <ChevronDown className={`h-4 w-4 transition-transform ${mobileProductsOpen ? "rotate-180" : ""}`} />
            </button>
            {mobileProductsOpen && (
              <div className="mb-1 flex flex-col gap-2 rounded-lg bg-white/[0.03] px-3 py-2">
                {CATEGORY_ORDER.map((cat) => {
                  const cc = CATEGORIES[cat];
                  return (
                    <div key={cat}>
                      <div className="pb-1 text-[11px] font-semibold text-neon-cyan">
                        {cc.zh} · {cc.tagline[lang]}
                      </div>
                      <div className="flex flex-col">
                        {productsInCategory(cat).map((key) => {
                          const p = BRAND.products[key];
                          return (
                            <a
                              key={key}
                              href={productHref(key)}
                              onClick={() => {
                                track("product_click", { key, where: "nav_mobile" });
                                setOpen(false);
                              }}
                              className="rounded-md px-2 py-1.5 text-sm text-slate-300 hover:bg-white/5 hover:text-white"
                            >
                              {p.zh} <span className="text-[11px] text-neon-cyan">{p.en}</span>
                            </a>
                          );
                        })}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}

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
