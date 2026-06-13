"use client";

import Image from "next/image";
import { Quote, Languages, Bot, MessageSquareText } from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";

function isRTL(text: string) {
  return /[\u0600-\u06FF]/.test(text);
}

export default function Cases() {
  const { t } = useLang();
  const c = t.cases;

  return (
    <section id="cases" className="relative py-24">
      <div className="pointer-events-none absolute left-1/2 top-10 -z-10 h-80 w-[760px] -translate-x-1/2 rounded-full bg-neon-cyan/8 blur-[130px]" />
      <div className="mx-auto max-w-6xl px-5">
        <Reveal className="mb-12 text-center">
          <span className="inline-flex items-center gap-1.5 rounded-full border border-neon-cyan/30 bg-neon-cyan/10 px-3 py-1 text-xs font-medium text-neon-cyan">
            {c.badge}
          </span>
          <h2 className="mt-4 text-3xl font-bold text-white md:text-4xl">{c.title}</h2>
          <p className="mx-auto mt-3 max-w-2xl text-slate-400">{c.subtitle}</p>
        </Reveal>

        {/* case cards */}
        <div className="grid gap-5 md:grid-cols-3">
          {c.items.map((it, i) => (
            <Reveal key={it.scene} delay={i * 0.08} className="h-full">
              <div className="card-hover group flex h-full flex-col overflow-hidden rounded-2xl border border-white/10 bg-ink-900/60">
                <div className="relative aspect-[16/10] overflow-hidden">
                  <Image
                    src={it.img}
                    alt={it.scene}
                    fill
                    sizes="(max-width: 768px) 100vw, 380px"
                    className="object-cover transition-transform duration-500 group-hover:scale-105"
                  />
                  <div className="absolute inset-0 bg-gradient-to-t from-ink-950 via-ink-950/30 to-transparent" />
                  <span className="absolute left-3 top-3 rounded-full border border-white/15 bg-black/45 px-2.5 py-1 text-[11px] font-medium text-slate-200 backdrop-blur">
                    {it.scene}
                  </span>
                  <div className="absolute bottom-3 left-3">
                    <span className="text-3xl font-black text-gradient">{it.metric}</span>
                    <span className="ml-1.5 text-xs text-slate-300">{it.metricLabel}</span>
                  </div>
                </div>
                <div className="flex flex-1 flex-col p-5">
                  <Quote className="h-5 w-5 text-neon-violet/60" />
                  <p className="mt-2 flex-1 text-sm leading-relaxed text-slate-300">{it.quote}</p>
                  <div className="mt-4 flex items-center gap-3 border-t border-white/5 pt-4">
                    <span className="grid h-9 w-9 place-items-center rounded-full bg-gradient-to-br from-neon-cyan/30 to-neon-violet/30 text-sm font-semibold text-white ring-1 ring-white/10">
                      {it.name.slice(0, 1)}
                    </span>
                    <span>
                      <span className="block text-sm font-medium text-white">{it.name}</span>
                      <span className="block text-xs text-slate-500">{it.role}</span>
                    </span>
                  </div>
                </div>
              </div>
            </Reveal>
          ))}
        </div>

        {/* multi-language gallery */}
        <Reveal className="mb-8 mt-20 text-center">
          <h3 className="inline-flex items-center gap-2 text-2xl font-bold text-white">
            <MessageSquareText className="h-6 w-6 text-neon-cyan" />
            {c.galleryTitle}
          </h3>
          <p className="mx-auto mt-2 max-w-2xl text-sm text-slate-400">{c.gallerySubtitle}</p>
        </Reveal>

        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {c.gallery.map((g, i) => {
            const rtl = isRTL(g.incoming) || isRTL(g.reply);
            return (
              <Reveal key={g.lang} delay={i * 0.06} className="h-full">
                <div className="flex h-full flex-col overflow-hidden rounded-2xl border border-white/10 bg-ink-900/70 shadow-xl">
                  {/* phone-style header */}
                  <div className="flex items-center gap-2 border-b border-white/10 bg-ink-800/60 px-3 py-2.5">
                    <span className="text-base leading-none">{g.flag}</span>
                    <span className="text-xs font-medium text-slate-200">{g.lang}</span>
                    <span className="ml-auto flex gap-1">
                      <span className="h-1.5 w-1.5 rounded-full bg-white/20" />
                      <span className="h-1.5 w-1.5 rounded-full bg-white/20" />
                      <span className="h-1.5 w-1.5 rounded-full bg-white/20" />
                    </span>
                  </div>

                  <div className="flex flex-1 flex-col gap-2.5 p-3" dir={rtl ? "rtl" : "ltr"}>
                    {/* incoming */}
                    <div className="mr-auto max-w-[88%] rounded-2xl rounded-tl-sm border border-white/10 bg-ink-800/80 px-3 py-2">
                      <p className="text-[13px] text-slate-100">{g.incoming}</p>
                      <div className="mt-1.5 flex items-start gap-1 border-t border-white/10 pt-1.5" dir="ltr">
                        <span className="inline-flex shrink-0 items-center gap-0.5 rounded bg-neon-cyan/15 px-1 py-0.5 text-[9px] font-medium text-neon-cyan">
                          <Languages className="h-2.5 w-2.5" />
                          {c.translatedTag}
                        </span>
                        <p className="text-[12px] text-neon-cyan/90">{g.translated}</p>
                      </div>
                    </div>

                    {/* AI auto-close reply */}
                    <div className="ml-auto max-w-[90%]">
                      <span className="mb-1 flex items-center justify-end gap-1 text-[9px] text-neon-cyan" dir="ltr">
                        <Bot className="h-2.5 w-2.5" />
                        {c.replyTag}
                      </span>
                      <div className="rounded-2xl rounded-tr-sm bg-gradient-to-r from-neon-cyan to-neon-violet px-3 py-2">
                        <p className="text-[13px] font-medium text-ink-950">{g.reply}</p>
                      </div>
                    </div>
                  </div>
                </div>
              </Reveal>
            );
          })}
        </div>

        <p className="mx-auto mt-8 max-w-3xl text-center text-[11px] text-slate-500">{c.disclaimer}</p>
      </div>
    </section>
  );
}
