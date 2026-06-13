"use client";

import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { useLang } from "./LanguageContext";

export interface LegalSection {
  h: { zh: string; en: string };
  p: { zh: string[]; en: string[] };
}

export default function LegalShell({
  title,
  updated,
  sections,
}: {
  title: { zh: string; en: string };
  updated: string;
  sections: LegalSection[];
}) {
  const { lang } = useLang();
  const zh = lang === "zh";

  return (
    <main className="relative min-h-screen px-5 py-16">
      <div className="mx-auto max-w-3xl">
        <Link
          href={zh ? "/" : "/en"}
          className="inline-flex items-center gap-1.5 text-sm text-slate-400 transition hover:text-white"
        >
          <ArrowLeft className="h-4 w-4" />
          {zh ? "返回首页" : "Back to home"}
        </Link>

        <h1 className="mt-6 text-3xl font-bold tracking-tight text-white md:text-4xl">
          {zh ? title.zh : title.en}
        </h1>
        <p className="mt-2 text-xs text-slate-500">
          {zh ? "最后更新" : "Last updated"}: {updated}
        </p>

        <div className="mt-10 space-y-8">
          {sections.map((s, i) => (
            <section key={i}>
              <h2 className="text-lg font-semibold text-white">{zh ? s.h.zh : s.h.en}</h2>
              <div className="mt-2 space-y-2">
                {(zh ? s.p.zh : s.p.en).map((para, j) => (
                  <p key={j} className="text-sm leading-relaxed text-slate-400">
                    {para}
                  </p>
                ))}
              </div>
            </section>
          ))}
        </div>
      </div>
    </main>
  );
}
