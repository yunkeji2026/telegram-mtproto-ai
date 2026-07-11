"use client";

import Link from "next/link";
import { useLang } from "./LanguageContext";
import BrandMark from "./BrandMark";
import { BRAND } from "@/lib/brand";

const anchors = ["#showcase", "#pricing", "#about", "#contact"];

export default function Footer() {
  const { t, lang } = useLang();
  const year = new Date().getFullYear();
  const zh = lang === "zh";
  // 锚点带上首页前缀：Footer 也出现在 /voice 等落地页，纯 #hash 在那里会失效
  const home = zh ? "/" : "/en";
  const landingLinks = [
    { href: zh ? "/voice" : "/en/voice", label: zh ? "幻声 · 声音克隆" : "VoiceX · Voice cloning" },
    { href: zh ? "/face" : "/en/face", label: zh ? "幻颜 · 实时换脸" : "FaceX · Live face swap" },
    { href: zh ? "/interpreting" : "/en/interpreting", label: zh ? "通译 · 克隆音同传" : "LingoX · Interpreting" },
  ];

  return (
    <footer className="border-t border-white/5 bg-ink-900/40">
      <div className="mx-auto max-w-7xl px-5 py-12">
        <div className="flex flex-col items-start justify-between gap-8 md:flex-row">
          <div className="max-w-sm">
            <div className="flex items-center gap-2">
              <BrandMark className="h-8 w-8" />
              <span className="font-semibold text-white">{BRAND.company.zh} {BRAND.company.en}</span>
            </div>
            <p className="mt-4 text-xs leading-relaxed text-slate-500">
              <span className="font-medium text-slate-400">{t.footer.disclaimerTitle}：</span>
              {t.footer.disclaimer}
            </p>
          </div>

          <div className="flex flex-col gap-4">
            <nav className="flex flex-wrap gap-x-8 gap-y-2">
              {t.footer.links.map((label, i) => (
                <a
                  key={label}
                  href={`${home}${anchors[i]}`}
                  className="text-sm text-slate-400 transition hover:text-white"
                >
                  {label}
                </a>
              ))}
            </nav>
            <nav className="flex flex-wrap gap-x-8 gap-y-2">
              {landingLinks.map((l) => (
                <Link key={l.href} href={l.href} className="text-sm text-slate-500 transition hover:text-neon-cyan">
                  {l.label}
                </Link>
              ))}
            </nav>
          </div>
        </div>

        <div className="mt-10 flex flex-col items-center gap-3 border-t border-white/5 pt-6 text-center text-xs text-slate-600">
          <div className="flex items-center gap-4">
            <Link href={zh ? "/privacy" : "/en/privacy"} className="transition hover:text-slate-300">
              {zh ? "隐私政策" : "Privacy"}
            </Link>
            <span className="text-slate-700">·</span>
            <Link href={zh ? "/terms" : "/en/terms"} className="transition hover:text-slate-300">
              {zh ? "服务条款" : "Terms"}
            </Link>
          </div>
          <div>© {year} {t.footer.rights}</div>
        </div>
      </div>
    </footer>
  );
}
