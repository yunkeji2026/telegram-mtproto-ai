"use client";

import { useMemo, useState } from "react";
import { motion } from "framer-motion";
import { Calculator, TrendingUp, Users, ArrowRight, Sparkles } from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import { CONTACT_URL } from "@/lib/site";
import { track } from "@/lib/track";

const LABOR_OPT = 0.6;
const CONV_UPLIFT = 0.35;
const DAYS = 30;

function fmt(n: number) {
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(Math.max(0, Math.round(n)));
}

function Slider({
  label,
  unit,
  min,
  max,
  step,
  value,
  onChange,
}: {
  label: string;
  unit: string;
  min: number;
  max: number;
  step: number;
  value: number;
  onChange: (v: number) => void;
}) {
  const pct = ((value - min) / (max - min)) * 100;
  return (
    <div>
      <div className="mb-1.5 flex items-baseline justify-between">
        <span className="text-sm text-slate-400">{label}</span>
        <span className="text-sm font-semibold text-white">
          {fmt(value)} <span className="text-xs font-normal text-slate-500">{unit}</span>
        </span>
      </div>
      <input
        type="range"
        aria-label={label}
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="roi-range w-full"
        style={{
          background: `linear-gradient(to right, #22d3ee ${pct}%, rgba(255,255,255,0.12) ${pct}%)`,
        }}
      />
    </div>
  );
}

export default function RoiCalculator() {
  const { t } = useLang();
  const r = t.roi;

  const [agents, setAgents] = useState(3);
  const [salary, setSalary] = useState(800);
  const [leads, setLeads] = useState(60);
  const [aov, setAov] = useState(50);
  const [conv, setConv] = useState(8);

  const calc = useMemo(() => {
    const planIdx = agents >= 9 || leads >= 150 ? 2 : 1;
    const plan = t.plans.items[planIdx];
    const planCost = Number(plan.priceMonthly) || 0;

    const laborSave = agents * salary * LABOR_OPT;
    const newConv = Math.min(conv * (1 + CONV_UPLIFT), 95);
    const extraRev = leads * DAYS * ((newConv - conv) / 100) * aov;
    const gain = laborSave + extraRev;
    const net = gain - planCost;
    const roi = planCost > 0 ? gain / planCost : 0;
    const yearNet = net * 12;

    const outBar = 100;
    const inBar = gain > 0 ? Math.max(4, (planCost / gain) * 100) : 4;

    return { plan, planCost, laborSave, extraRev, gain, net, roi, yearNet, inBar, outBar };
  }, [agents, salary, leads, aov, conv, t.plans.items]);

  return (
    <section className="relative py-8">
      <div className="mx-auto max-w-5xl px-5">
        <Reveal className="mb-8 text-center">
          <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-400/30 bg-emerald-400/10 px-3 py-1 text-xs font-medium text-emerald-300">
            <Calculator className="h-3.5 w-3.5" />
            {r.badge}
          </span>
          <h3 className="mt-3 text-2xl font-bold text-white md:text-3xl">{r.title}</h3>
          <p className="mx-auto mt-2 max-w-2xl text-sm text-slate-400">{r.subtitle}</p>
        </Reveal>

        <Reveal>
          <div className="grid gap-6 rounded-3xl border border-white/10 bg-ink-900/60 p-6 lg:grid-cols-2 lg:p-8">
            {/* inputs */}
            <div className="space-y-5">
              <Slider label={r.inputs.agents} unit={r.units.agents} min={1} max={30} step={1} value={agents} onChange={setAgents} />
              <Slider label={r.inputs.salary} unit={r.units.salary} min={200} max={3000} step={50} value={salary} onChange={setSalary} />
              <Slider label={r.inputs.leads} unit={r.units.leads} min={5} max={500} step={5} value={leads} onChange={setLeads} />
              <Slider label={r.inputs.aov} unit={r.units.aov} min={5} max={2000} step={5} value={aov} onChange={setAov} />
              <Slider label={r.inputs.conv} unit={r.units.conv} min={1} max={40} step={1} value={conv} onChange={setConv} />

              <div className="rounded-xl border border-white/5 bg-ink-950/40 p-3">
                <p className="mb-1.5 text-[11px] font-medium text-slate-400">{r.assumptionsTitle}</p>
                <ul className="space-y-1">
                  {r.assumptions.map((a) => (
                    <li key={a} className="flex items-start gap-1.5 text-[11px] leading-relaxed text-slate-500">
                      <span className="mt-1 h-1 w-1 shrink-0 rounded-full bg-neon-cyan/60" />
                      {a}
                    </li>
                  ))}
                </ul>
              </div>
            </div>

            {/* results */}
            <div className="flex flex-col">
              <div className="rounded-2xl border border-neon-cyan/30 bg-gradient-to-br from-neon-cyan/10 to-neon-violet/10 p-6 text-center">
                <p className="text-sm text-slate-300">{r.resultNetLabel}</p>
                <motion.p
                  key={Math.round(calc.net)}
                  initial={{ opacity: 0.4, y: 6 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.25 }}
                  className="mt-1 text-4xl font-black text-white md:text-5xl"
                >
                  +{fmt(calc.net)}
                  <span className="ml-1 text-base font-medium text-slate-400">USDT{r.perMonth}</span>
                </motion.p>

                {/* in vs out bars */}
                <div className="mt-5 space-y-2 text-left">
                  <div className="flex items-center gap-2">
                    <span className="w-16 shrink-0 text-[11px] text-slate-400">{r.planLabel}</span>
                    <div className="h-2.5 flex-1 overflow-hidden rounded-full bg-white/10">
                      <div className="h-full rounded-full bg-slate-500" style={{ width: `${calc.inBar}%` }} />
                    </div>
                    <span className="w-20 shrink-0 text-right text-[11px] tabular-nums text-slate-400">{fmt(calc.planCost)}</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="w-16 shrink-0 text-[11px] text-neon-cyan">{r.resultRoiLabel}</span>
                    <div className="h-2.5 flex-1 overflow-hidden rounded-full bg-white/10">
                      <motion.div
                        className="h-full rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet"
                        animate={{ width: `${calc.outBar}%` }}
                        transition={{ duration: 0.4 }}
                      />
                    </div>
                    <span className="w-20 shrink-0 text-right text-[11px] font-semibold tabular-nums text-neon-cyan">
                      {calc.roi.toFixed(1)}×
                    </span>
                  </div>
                </div>
              </div>

              <div className="mt-4 grid grid-cols-2 gap-3">
                <div className="rounded-xl border border-white/10 bg-ink-900/60 p-4">
                  <span className="flex items-center gap-1.5 text-xs text-slate-400">
                    <Users className="h-3.5 w-3.5 text-neon-cyan" />
                    {r.resultSaveLabel}
                  </span>
                  <p className="mt-1 text-xl font-bold text-white">{fmt(calc.laborSave)}</p>
                </div>
                <div className="rounded-xl border border-white/10 bg-ink-900/60 p-4">
                  <span className="flex items-center gap-1.5 text-xs text-slate-400">
                    <TrendingUp className="h-3.5 w-3.5 text-emerald-400" />
                    {r.resultRevenueLabel}
                  </span>
                  <p className="mt-1 text-xl font-bold text-white">{fmt(calc.extraRev)}</p>
                </div>
              </div>

              <div className="mt-3 flex items-center justify-between rounded-xl border border-white/10 bg-ink-900/40 px-4 py-3">
                <span className="text-xs text-slate-400">{r.resultYearLabel}</span>
                <span className="text-lg font-bold text-gradient">+{fmt(calc.yearNet)} USDT</span>
              </div>

              <div className="mt-3 flex items-center justify-between rounded-xl border border-neon-cyan/20 bg-neon-cyan/[0.05] px-4 py-3">
                <span className="flex items-center gap-1.5 text-xs text-slate-300">
                  <Sparkles className="h-3.5 w-3.5 text-neon-cyan" />
                  {r.planLabel}
                </span>
                <span className="text-sm font-semibold text-white">
                  {calc.plan.name} · {calc.plan.priceMonthly} USDT{r.perMonth}
                </span>
              </div>

              <a
                href={CONTACT_URL}
                target="_blank"
                rel="noreferrer"
                onClick={() => track("cta_click", { where: "roi", roi: calc.roi.toFixed(1), net: Math.round(calc.net) })}
                className="group mt-4 inline-flex items-center justify-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-6 py-3 text-sm font-semibold text-ink-950 transition hover:opacity-90"
              >
                {r.cta}
                <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-1" />
              </a>

              <p className="mt-3 text-center text-[11px] leading-relaxed text-slate-500">{r.disclaimer}</p>
            </div>
          </div>
        </Reveal>
      </div>
    </section>
  );
}
