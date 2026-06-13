"use client";

import { Check, ArrowRight } from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import { track } from "@/lib/track";
import WaveformPlayer from "./WaveformPlayer";
import DigitalHumanCard from "./DigitalHumanCard";
import DeployDiagram from "./DeployDiagram";

function Row({
  reverse,
  badge,
  title,
  desc,
  features,
  media,
  ctaLabel,
  trackId,
}: {
  reverse?: boolean;
  badge: string;
  title: string;
  desc: string;
  features: string[];
  media: React.ReactNode;
  ctaLabel: string;
  trackId: string;
}) {
  return (
    <Reveal>
      <div className="grid items-center gap-10 lg:grid-cols-2">
        <div className={reverse ? "lg:order-2" : ""}>{media}</div>
        <div className={reverse ? "lg:order-1" : ""}>
          <span className="inline-flex items-center gap-1.5 rounded-full border border-neon-cyan/30 bg-neon-cyan/10 px-3 py-1 text-xs font-medium text-neon-cyan">
            {badge}
          </span>
          <h3 className="mt-4 text-2xl font-bold text-white md:text-3xl">{title}</h3>
          <p className="mt-3 max-w-xl text-slate-400">{desc}</p>
          <ul className="mt-5 grid max-w-md grid-cols-2 gap-2.5">
            {features.map((f) => (
              <li key={f} className="flex items-center gap-2 text-sm text-slate-300">
                <Check className="h-4 w-4 shrink-0 text-neon-cyan" />
                {f}
              </li>
            ))}
          </ul>
          <a
            href="#pricing"
            onClick={() => track("cta_click", { where: "showcase", which: trackId })}
            className="group mt-6 inline-flex items-center gap-1.5 text-sm font-medium text-neon-cyan transition hover:text-white"
          >
            {ctaLabel}
            <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-1" />
          </a>
        </div>
      </div>
    </Reveal>
  );
}

export default function Showcase() {
  const { t } = useLang();
  const more = t.nav.pricing;

  return (
    <section id="showcase" className="relative bg-white/[0.015] py-24">
      <div className="pointer-events-none absolute inset-0 -z-10 bg-[radial-gradient(ellipse_at_top,rgba(34,211,238,0.06),transparent_60%)]" />
      <div className="mx-auto max-w-6xl px-5">
        <Reveal className="mb-16 text-center">
          <h2 className="text-3xl font-bold text-white md:text-4xl">{t.showcaseSection.title}</h2>
          <p className="mx-auto mt-3 max-w-2xl text-slate-400">{t.showcaseSection.subtitle}</p>
        </Reveal>

        <div className="space-y-24">
          <Row
            badge={t.voiceDemo.badge}
            title={t.voiceDemo.title}
            desc={t.voiceDemo.desc}
            features={t.voiceDemo.features}
            media={<WaveformPlayer />}
            ctaLabel={more}
            trackId="voice"
          />
          <Row
            reverse
            badge={t.digitalDemo.badge}
            title={t.digitalDemo.title}
            desc={t.digitalDemo.desc}
            features={t.digitalDemo.features}
            media={<DigitalHumanCard />}
            ctaLabel={more}
            trackId="digital"
          />
          <Row
            badge={t.deployDemo.badge}
            title={t.deployDemo.title}
            desc={t.deployDemo.desc}
            features={t.deployDemo.features}
            media={<DeployDiagram />}
            ctaLabel={more}
            trackId="deploy"
          />
        </div>
      </div>
    </section>
  );
}
