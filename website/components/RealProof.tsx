"use client";

import {
  PlayCircle,
  AudioLines,
  BadgeCheck,
  CalendarClock,
  Send,
  Fingerprint,
  Check,
  ArrowRight,
  Radio,
} from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import BeforeAfter from "./fx/BeforeAfter";
import { AudioClip, VideoClip } from "./fx/MediaClips";
import { ENGINE } from "@/lib/engineContent";
import { CONTACT_URL, CHANNEL_URL } from "@/lib/site";
import { track } from "@/lib/track";

const LAYER_ICONS: Record<string, typeof PlayCircle> = {
  playcircle: PlayCircle,
  waveform: AudioLines,
  badgecheck: BadgeCheck,
  calendar: CalendarClock,
  send: Send,
};

export default function RealProof() {
  const { lang } = useLang();
  const p = ENGINE.proof;

  return (
    <section id="proof" className="relative border-y border-white/5 bg-white/[0.015] py-24">
      <div className="pointer-events-none absolute inset-0 -z-10 bg-[radial-gradient(ellipse_at_top,rgba(139,92,246,0.08),transparent_60%)]" />
      <div className="mx-auto max-w-6xl px-5">
        <Reveal className="mx-auto max-w-3xl text-center">
          <span className="inline-flex items-center gap-1.5 rounded-full border border-neon-cyan/30 bg-neon-cyan/10 px-3 py-1 text-xs font-medium text-neon-cyan">
            <Radio className="h-3.5 w-3.5" />
            {p.kicker[lang]}
          </span>
          <h2 className="mt-4 text-3xl font-bold text-white md:text-4xl">{p.title[lang]}</h2>
          <p className="mx-auto mt-3 max-w-2xl text-slate-400">{p.subtitle[lang]}</p>
        </Reveal>

        {/* 5 proof layers */}
        <div className="mt-10 grid gap-3 sm:grid-cols-3 lg:grid-cols-5">
          {p.layers.map((l, i) => {
            const Icon = LAYER_ICONS[l.icon] ?? PlayCircle;
            return (
              <Reveal key={l.title.en} delay={i * 0.05}>
                <div className="flex h-full flex-col items-center rounded-2xl border border-white/10 bg-ink-900/50 p-4 text-center">
                  <span className="grid h-10 w-10 place-items-center rounded-xl bg-gradient-to-br from-neon-cyan/20 to-neon-violet/20 text-neon-cyan">
                    <Icon className="h-5 w-5" />
                  </span>
                  <div className="mt-3 text-sm font-semibold text-white">{l.title[lang]}</div>
                  <p className="mt-1 text-[11px] leading-relaxed text-slate-400">{l.desc[lang]}</p>
                </div>
              </Reveal>
            );
          })}
        </div>

        {/* metrics strip */}
        <Reveal className="mt-10">
          <div className="rounded-2xl border border-white/10 bg-ink-900/50 p-6">
            <p className="mb-5 text-center text-sm font-medium text-slate-300">{p.metricsTitle[lang]}</p>
            <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6">
              {p.metrics.map((m) => (
                <div key={m.label.en} className="text-center">
                  <div className="text-gradient text-2xl font-bold">{m.value}</div>
                  <div className="mt-1 text-[11px] leading-tight text-slate-400">{m.label[lang]}</div>
                </div>
              ))}
            </div>
            <p className="mx-auto mt-5 max-w-3xl text-center text-[11px] text-slate-500">{p.metricsNote[lang]}</p>
          </div>
        </Reveal>

        {/* real media gallery */}
        <Reveal className="mb-8 mt-16 text-center">
          <h3 className="text-2xl font-bold text-white">{p.galleryTitle[lang]}</h3>
          <p className="mx-auto mt-2 max-w-2xl text-sm text-slate-400">{p.gallerySubtitle[lang]}</p>
        </Reveal>

        <div className="grid gap-5 lg:grid-cols-3">
          {/* audio */}
          <Reveal>
            <div className="flex h-full flex-col rounded-2xl border border-white/10 bg-ink-900/60 p-5">
              <div className="flex items-center gap-2 text-white">
                <AudioLines className="h-5 w-5 text-neon-cyan" />
                <span className="font-semibold">{p.audioTitle[lang]}</span>
              </div>
              <p className="mt-1.5 text-xs text-slate-400">{p.audioDesc[lang]}</p>
              <div className="mt-4 space-y-2.5">
                {p.audioClips.map((clip) => (
                  <AudioClip key={clip.src} label={clip.label[lang]} src={clip.src} />
                ))}
              </div>
              <p className="mt-auto pt-4 text-[11px] leading-relaxed text-slate-500">{p.mediaRealNote[lang]}</p>
            </div>
          </Reveal>

          {/* video */}
          <Reveal delay={0.06}>
            <div className="flex h-full flex-col rounded-2xl border border-white/10 bg-ink-900/60 p-5">
              <div className="flex items-center gap-2 text-white">
                <PlayCircle className="h-5 w-5 text-neon-violet" />
                <span className="font-semibold">{p.videoTitle[lang]}</span>
              </div>
              <p className="mt-1.5 text-xs text-slate-400">{p.videoDesc[lang]}</p>
              <div className="mt-4">
                <VideoClip src={p.videoSrc} poster={p.videoPoster} pending={p.mediaPending[lang]} />
              </div>
            </div>
          </Reveal>

          {/* before/after swap */}
          <Reveal delay={0.12}>
            <div className="flex h-full flex-col rounded-2xl border border-white/10 bg-ink-900/60 p-5">
              <div className="flex items-center gap-2 text-white">
                <BadgeCheck className="h-5 w-5 text-emerald-400" />
                <span className="font-semibold">{p.swapTitle[lang]}</span>
              </div>
              <p className="mt-1.5 text-xs text-slate-400">{p.swapDesc[lang]}</p>
              <div className="mt-4">
                <BeforeAfter
                  before={p.swapBefore}
                  after={p.swapAfter}
                  beforeLabel={p.beforeLabel[lang]}
                  afterLabel={p.afterLabel[lang]}
                  hint={p.dragHint[lang]}
                />
              </div>
            </div>
          </Reveal>
        </div>

        {/* verify + live demo */}
        <div className="mt-16 grid gap-5 lg:grid-cols-2">
          <Reveal>
            <div className="flex h-full flex-col rounded-2xl border border-emerald-400/20 bg-emerald-400/[0.04] p-6">
              <span className="inline-flex w-fit items-center gap-2 rounded-full border border-emerald-400/30 bg-emerald-400/10 px-3 py-1 text-xs font-medium text-emerald-300">
                <Fingerprint className="h-3.5 w-3.5" />
                {p.verifyTitle[lang]}
              </span>
              <p className="mt-4 text-sm leading-relaxed text-slate-300">{p.verifyDesc[lang]}</p>
              <ul className="mt-5 grid gap-2.5">
                {p.verifyPoints.map((pt) => (
                  <li key={pt.en} className="flex items-start gap-2 text-sm text-slate-200">
                    <Check className="mt-0.5 h-4 w-4 shrink-0 text-emerald-400" />
                    {pt[lang]}
                  </li>
                ))}
              </ul>
            </div>
          </Reveal>

          <Reveal delay={0.06}>
            <div className="relative flex h-full flex-col overflow-hidden rounded-2xl border border-neon-cyan/30 bg-gradient-to-br from-neon-cyan/[0.08] to-neon-violet/[0.08] p-6">
              <span className="inline-flex w-fit items-center gap-2 rounded-full border border-neon-cyan/40 bg-neon-cyan/10 px-3 py-1 text-xs font-semibold text-neon-cyan">
                <CalendarClock className="h-3.5 w-3.5" />
                {p.liveTitle[lang]}
              </span>
              <p className="mt-4 text-sm leading-relaxed text-slate-200">{p.liveDesc[lang]}</p>
              <ul className="mt-5 grid gap-2.5">
                {p.livePoints.map((pt) => (
                  <li key={pt.en} className="flex items-start gap-2 text-sm text-slate-200">
                    <Check className="mt-0.5 h-4 w-4 shrink-0 text-neon-cyan" />
                    {pt[lang]}
                  </li>
                ))}
              </ul>
              <div className="mt-6 flex flex-wrap gap-3">
                <a
                  href={CONTACT_URL}
                  target="_blank"
                  rel="noreferrer"
                  onClick={() => track("cta_click", { where: "proof_live_demo" })}
                  className="group inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-6 py-3 text-sm font-semibold text-ink-950 transition hover:opacity-90"
                >
                  {p.liveCta[lang]}
                  <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-1" />
                </a>
                <a
                  href={CHANNEL_URL}
                  target="_blank"
                  rel="noreferrer"
                  onClick={() => track("cta_click", { where: "proof_channel" })}
                  className="inline-flex items-center gap-2 rounded-full border border-white/20 px-6 py-3 text-sm font-semibold text-white transition hover:bg-white/5"
                >
                  <Send className="h-4 w-4" />
                  {p.feedCta[lang]}
                </a>
              </div>
            </div>
          </Reveal>
        </div>

        <p className="mx-auto mt-10 max-w-3xl text-center text-[11px] leading-relaxed text-slate-500">{p.disclaimer[lang]}</p>
      </div>
    </section>
  );
}
