"use client";

import { useRef, useState } from "react";
import type { Dict, Solution } from "@/lib/content";
import { CHANNEL_URL, GROUP_URL, CONTACT_URL } from "@/lib/site";
import { track } from "@/lib/track";
import type { View } from "./routing";

const HUAYING_IDS = ["faceswap", "voice", "digital-human", "video-dubbing"];
const LINGXI_IDS = ["translate", "private-ai"];

/* ───────────────────────── shared bits ───────────────────────── */

export function SectionTitle({ icon, title, sub }: { icon: string; title: string; sub?: string }) {
  return (
    <div className="mb-3 mt-6 first:mt-0">
      <div className="flex items-center gap-2 text-base font-bold text-white">
        <span>{icon}</span>
        <span>{title}</span>
      </div>
      {sub && <p className="mt-1 text-xs leading-relaxed text-slate-400">{sub}</p>}
    </div>
  );
}

function Chip({ children }: { children: React.ReactNode }) {
  return <span className="rounded-md bg-slate-800 px-2 py-0.5 text-[11px] text-cyan-300">{children}</span>;
}

function CtaButton({ label, onClick, href }: { label: string; onClick?: () => void; href?: string }) {
  const cls =
    "mt-4 block w-full rounded-xl bg-gradient-to-r from-cyan-400 to-violet-500 py-3 text-center text-sm font-semibold text-slate-950 active:scale-[0.99] transition";
  if (href) {
    return (
      <a href={href} target="_blank" rel="noreferrer" className={cls}>
        {label}
      </a>
    );
  }
  return (
    <button onClick={onClick} className={cls}>
      {label}
    </button>
  );
}

const EMOJI: Record<string, string> = {
  voice: "🎙",
  faceswap: "🎭",
  translate: "💬",
  "private-ai": "🔐",
  "digital-human": "👤",
  "video-dubbing": "🎬",
};

function SolutionCard({ s }: { s: Solution }) {
  return (
    <div className={`rounded-xl border p-3 ${s.highlight ? "border-cyan-500/50 bg-cyan-500/5" : "border-slate-800 bg-slate-900/40"}`}>
      <div className="flex items-center justify-between">
        <div className="text-sm font-semibold text-white">
          {EMOJI[s.id] ?? "📦"} {s.title}
        </div>
        <span className="rounded-full border border-slate-700 px-2 py-0.5 text-[10px] text-slate-400">{s.tag}</span>
      </div>
      <div className="mt-1 text-xs leading-relaxed text-slate-400">{s.desc}</div>
      <div className="mt-2 flex flex-wrap gap-1">
        {s.pricing.slice(0, 3).map((p, i) => (
          <Chip key={i}>
            {p.plan} {p.price}
          </Chip>
        ))}
      </div>
    </div>
  );
}

/* ───────────────────────── Before / After slider ───────────────────────── */

function BeforeAfter({ before, after, t }: { before: string; after: string; t: Dict }) {
  const [pos, setPos] = useState(55);
  return (
    <div className="relative overflow-hidden rounded-2xl border border-slate-800 bg-slate-900">
      <div className="relative aspect-[4/3] w-full select-none">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={before} alt={t.swap.before} className="absolute inset-0 h-full w-full object-cover" draggable={false} />
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={after}
          alt={t.swap.after}
          className="absolute inset-0 h-full w-full object-cover"
          style={{ clipPath: `inset(0 ${100 - pos}% 0 0)` }}
          draggable={false}
        />
        <span className="absolute left-2 top-2 rounded bg-black/60 px-2 py-0.5 text-[10px] text-slate-200">{t.swap.before}</span>
        <span className="absolute right-2 top-2 rounded bg-cyan-500/80 px-2 py-0.5 text-[10px] font-medium text-slate-950">{t.swap.after}</span>
        <div className="absolute inset-y-0 w-0.5 bg-cyan-400" style={{ left: `${pos}%` }}>
          <div className="absolute top-1/2 -ml-3 -mt-3 h-6 w-6 -translate-y-1/2 rounded-full border-2 border-cyan-400 bg-slate-950 text-center text-[10px] leading-5 text-cyan-300">⇄</div>
        </div>
      </div>
      <input
        type="range"
        min={2}
        max={98}
        value={pos}
        onChange={(e) => setPos(Number(e.target.value))}
        className="absolute inset-x-0 bottom-0 h-full w-full cursor-ew-resize opacity-0"
        aria-label={t.swap.dragHint}
      />
      <div className="bg-slate-950/80 py-1.5 text-center text-[11px] text-slate-400">{t.swap.dragHint}</div>
    </div>
  );
}

/* ───────────────────────── Chat theater (灵犀) ───────────────────────── */

function ChatTheater({ t }: { t: Dict }) {
  const d = t.autochat.demo;
  return (
    <div className="rounded-2xl border border-violet-500/40 bg-slate-900/60 p-3">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-xs font-semibold text-violet-200">📥 {d.inbox}</span>
        <span className="rounded-full bg-violet-500/20 px-2 py-0.5 text-[10px] text-violet-200">{d.autoTag}</span>
      </div>
      {/* incoming */}
      <div className="mb-2">
        <div className="text-[10px] text-slate-500">
          {d.incoming.flag} {d.incoming.name}
        </div>
        <div className="mt-0.5 inline-block max-w-[88%] rounded-2xl rounded-tl-sm bg-slate-800 px-3 py-2 text-sm text-slate-100">
          {d.incoming.text}
          <div className="mt-1 border-t border-slate-700/60 pt-1 text-[11px] text-cyan-300">↳ {d.incoming.translated}</div>
        </div>
      </div>
      {/* reply */}
      <div className="text-right">
        <div className="text-[10px] text-slate-500">{d.personaName} · {d.translatedTag}</div>
        <div className="mt-0.5 inline-block max-w-[88%] rounded-2xl rounded-tr-sm bg-gradient-to-br from-cyan-500 to-violet-500 px-3 py-2 text-left text-sm text-slate-950">
          {d.reply.text}
          <div className="mt-1 border-t border-black/20 pt-1 text-[11px] text-slate-800">↳ {d.reply.translated}</div>
        </div>
        <div className="mt-1 flex items-center justify-end gap-2 text-[10px] text-slate-500">
          <span className="rounded-full bg-slate-800 px-2 py-0.5 text-cyan-300">🎙 {d.voiceLen}</span>
          <span className="rounded-full bg-slate-800 px-2 py-0.5">{d.voiceTag}</span>
        </div>
      </div>
    </div>
  );
}

/* ───────────────────────── view bodies ───────────────────────── */

export function HomeView({ t, zh, onGo }: { t: Dict; zh: boolean; onGo: (v: View) => void }) {
  return (
    <div>
      {/* hero */}
      <div className="rounded-2xl border border-cyan-700/30 bg-gradient-to-br from-cyan-500/10 via-slate-900 to-violet-500/10 p-4">
        <div className="text-[11px] font-medium text-cyan-300">{zh ? "灵动智能 · 华丽呈现" : "Intelligence, gracefully delivered"}</div>
        <h1 className="mt-1 text-xl font-extrabold leading-snug text-white">
          {zh ? "AI 自动成交 · 实时换脸换声" : "AI auto-closing · real-time face/voice swap"}
        </h1>
        <p className="mt-1.5 text-xs leading-relaxed text-slate-300">
          {zh
            ? "华影 LiveAvatar 看得见的分身，灵犀 SoulSync 听得懂的对话 —— 私有部署、数据不出网、USDT 结算。"
            : "HuaYing LiveAvatar you can see, LingXi SoulSync that understands — private, off-net, USDT."}
        </p>
        <div className="mt-3 grid grid-cols-4 gap-1.5">
          {t.hero.stats.map((s) => (
            <div key={s.label} className="rounded-lg bg-slate-950/50 px-1 py-2 text-center">
              <div className="text-sm font-bold text-cyan-300">{s.value}</div>
              <div className="mt-0.5 text-[9px] leading-tight text-slate-400">{s.label}</div>
            </div>
          ))}
        </div>
      </div>

      {/* two product lines */}
      <SectionTitle icon="🧭" title={zh ? "两大产品线" : "Two product lines"} />
      <div className="grid grid-cols-2 gap-2">
        <button onClick={() => onGo("liveavatar")} className="rounded-2xl border border-slate-800 bg-slate-900/60 p-3 text-left active:scale-[0.98] transition">
          <div className="text-2xl">🎭</div>
          <div className="mt-1 text-sm font-bold text-white">华影 LiveAvatar</div>
          <div className="mt-0.5 text-[11px] leading-snug text-slate-400">{zh ? "实时换脸换声 · 数字人 · 视频翻译配音" : "Live swap · digital human · dubbing"}</div>
          <div className="mt-2 text-[11px] font-medium text-cyan-300">{zh ? "查看 →" : "Explore →"}</div>
        </button>
        <button onClick={() => onGo("soulsync")} className="rounded-2xl border border-slate-800 bg-slate-900/60 p-3 text-left active:scale-[0.98] transition">
          <div className="text-2xl">💬</div>
          <div className="mt-1 text-sm font-bold text-white">灵犀 SoulSync</div>
          <div className="mt-0.5 text-[11px] leading-snug text-slate-400">{zh ? "AI 自动成交 · 拟人翻译 · AI 伴侣" : "AI closing · human-like translation"}</div>
          <div className="mt-2 text-[11px] font-medium text-violet-300">{zh ? "查看 →" : "Explore →"}</div>
        </button>
      </div>

      {/* quick links */}
      <SectionTitle icon="🔗" title={zh ? "快捷入口" : "Quick links"} />
      <div className="grid grid-cols-3 gap-2">
        <a href={CHANNEL_URL} target="_blank" rel="noreferrer" className="rounded-xl border border-slate-700 bg-slate-900/60 px-2 py-3 text-center text-xs">📢 {zh ? "官方频道" : "Channel"}</a>
        <a href={GROUP_URL} target="_blank" rel="noreferrer" className="rounded-xl border border-slate-700 bg-slate-900/60 px-2 py-3 text-center text-xs">💬 {zh ? "交流群" : "Group"}</a>
        <a href={CONTACT_URL} target="_blank" rel="noreferrer" className="rounded-xl border border-slate-700 bg-slate-900/60 px-2 py-3 text-center text-xs">👤 {zh ? "人工客服" : "Support"}</a>
      </div>
    </div>
  );
}

export function LiveAvatarView({ t, zh, onContact }: { t: Dict; zh: boolean; onContact: (interest: string) => void }) {
  const sols = t.solutions.filter((s) => HUAYING_IDS.includes(s.id));
  return (
    <div>
      <SectionTitle icon="🎭" title={zh ? "华影 LiveAvatar · 看得见的分身" : "HuaYing LiveAvatar"} sub={t.realtime.subtitle} />
      <BeforeAfter before="/showcase/live-before.png" after="/showcase/live-after.png" t={t} />

      <SectionTitle icon="⚡" title={zh ? "为什么是真·实时" : "Why true real-time"} />
      <div className="grid grid-cols-2 gap-2">
        {t.realtime.features.map((f) => (
          <div key={f.title} className="rounded-xl border border-slate-800 bg-slate-900/40 p-3">
            <div className="text-sm font-semibold text-white">{f.title}</div>
            <div className="mt-1 text-[11px] leading-relaxed text-slate-400">{f.desc}</div>
          </div>
        ))}
      </div>

      <SectionTitle icon="📦" title={zh ? "六项形象能力" : "Avatar capabilities"} />
      <div className="space-y-2">
        {sols.map((s) => (
          <SolutionCard key={s.id} s={s} />
        ))}
      </div>

      <SectionTitle icon="🛠" title={t.realtime.plansTitle} sub={t.realtime.plansNote} />
      <div className="space-y-2">
        {t.realtime.plans.map((p) => (
          <div key={p.name} className={`rounded-xl border p-3 ${p.highlight ? "border-cyan-500/60 bg-cyan-500/5" : "border-slate-800 bg-slate-900/40"}`}>
            <div className="flex items-center justify-between">
              <span className="text-sm font-semibold text-white">{p.name}</span>
              <span className="text-sm font-bold text-cyan-300">{p.price}</span>
            </div>
            <div className="mt-0.5 text-[10px] text-slate-500">{p.unit}</div>
            <ul className="mt-1.5 space-y-0.5">
              {p.specs.map((sp) => (
                <li key={sp} className="text-[11px] text-slate-400">· {sp}</li>
              ))}
            </ul>
          </div>
        ))}
      </div>
      <p className="mt-2 text-[11px] text-amber-300/80">⏳ {t.realtime.availability}</p>

      <CtaButton label={zh ? "🎬 预约换脸演示 / 咨询" : "🎬 Book a swap demo"} onClick={() => onContact(zh ? "华影 · 实时换脸咨询" : "LiveAvatar demo")} />
    </div>
  );
}

export function SoulSyncView({ t, zh, onContact }: { t: Dict; zh: boolean; onContact: (interest: string) => void }) {
  const sols = t.solutions.filter((s) => LINGXI_IDS.includes(s.id));
  return (
    <div>
      <SectionTitle icon="💬" title={zh ? "灵犀 SoulSync · AI 自动成交" : "LingXi SoulSync · AI closing"} sub={t.autochat.subtitle} />
      <ChatTheater t={t} />

      <SectionTitle icon="✨" title={zh ? "四大能力" : "Four capabilities"} />
      <div className="grid grid-cols-2 gap-2">
        {t.autochat.features.map((f) => (
          <div key={f.title} className="rounded-xl border border-slate-800 bg-slate-900/40 p-3">
            <div className="text-sm font-semibold text-white">{f.title}</div>
            <div className="mt-1 text-[11px] leading-relaxed text-slate-400">{f.desc}</div>
          </div>
        ))}
      </div>

      <SectionTitle icon="🆚" title={t.autochat.compareTitle} sub={t.autochat.compareNote} />
      <div className="space-y-2">
        {t.autochat.compare.map((c, i) => (
          <div key={i} className="rounded-xl border border-slate-800 bg-slate-900/40 p-3">
            <div className="text-[11px] text-slate-500">“{c.src}”</div>
            <div className="mt-1.5 flex items-start gap-1.5 text-[11px]">
              <span className="rounded bg-red-500/15 px-1.5 py-0.5 text-red-300">{t.autochat.badLabel}</span>
              <span className="text-slate-400 line-through decoration-red-500/40">{c.bad}</span>
            </div>
            <div className="mt-1 flex items-start gap-1.5 text-[11px]">
              <span className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-emerald-300">{t.autochat.goodLabel}</span>
              <span className="text-slate-200">{c.good}</span>
            </div>
          </div>
        ))}
      </div>

      <SectionTitle icon="🌐" title={zh ? "已聚合平台" : "Platforms unified"} />
      <div className="flex flex-wrap gap-1.5">
        {t.trust.platforms.map((p) => (
          <span key={p} className="rounded-lg border border-slate-700 bg-slate-900/60 px-2.5 py-1 text-[11px] text-slate-300">{p}</span>
        ))}
      </div>

      <SectionTitle icon="📦" title={zh ? "对话能力" : "Conversation"} />
      <div className="space-y-2">
        {sols.map((s) => (
          <SolutionCard key={s.id} s={s} />
        ))}
      </div>

      <CtaButton label={zh ? "💬 免费试用 AI 成交" : "💬 Try AI closing free"} onClick={() => onContact(zh ? "灵犀 · AI 成交试用" : "SoulSync trial")} />
    </div>
  );
}

export function PricingView({
  t,
  zh,
  gate,
  initData,
  verifying,
  onVerify,
}: {
  t: Dict;
  zh: boolean;
  gate: { channel: boolean; group: boolean; code: string; checked: boolean };
  initData: string;
  verifying: boolean;
  onVerify: () => void;
}) {
  return (
    <div>
      {/* unlock gate front and center */}
      <div id="unlock" className="rounded-2xl border border-violet-500/40 bg-gradient-to-br from-violet-500/10 to-cyan-500/10 p-3">
        {gate.channel && gate.group && gate.code ? (
          <div className="text-center">
            <div className="text-sm font-semibold text-violet-200">🎉 {zh ? "已解锁专属优惠" : "Offer unlocked"}</div>
            <div className="mx-auto mt-2 inline-flex flex-col items-center rounded-xl border border-cyan-400/40 bg-slate-950/70 px-6 py-3">
              <span className="text-[11px] text-slate-400">{zh ? "你的专属一次性折扣码" : "Your one-time code"}</span>
              <span className="font-mono text-2xl font-bold tracking-widest text-cyan-300">{gate.code}</span>
            </div>
            <p className="mt-2 text-[11px] text-slate-400">{zh ? "把折扣码发给客服即可享专属价（仅限一次）" : "Send to support for your exclusive price (one-time)"}</p>
            <a href={CONTACT_URL} target="_blank" rel="noreferrer" className="mt-3 inline-block rounded-xl bg-gradient-to-r from-cyan-400 to-violet-500 px-6 py-2 text-sm font-semibold text-slate-950">
              {zh ? "🚀 联系客服领取" : "🚀 Claim with support"}
            </a>
          </div>
        ) : (
          <>
            <div className="text-sm font-semibold text-violet-200">🔓 {zh ? "关注频道 + 进群，解锁专属折扣码" : "Join channel + group to unlock a code"}</div>
            <div className="mt-2 grid grid-cols-2 gap-2">
              <a href={CHANNEL_URL} target="_blank" rel="noreferrer" className={`rounded-xl px-2 py-2 text-center text-xs font-medium ${gate.channel ? "border border-emerald-500/50 bg-emerald-500/10 text-emerald-300" : "bg-cyan-500 text-slate-950"}`}>
                {gate.channel ? "✅ " : "① "}
                {zh ? "关注频道" : "Channel"}
              </a>
              <a href={GROUP_URL} target="_blank" rel="noreferrer" className={`rounded-xl px-2 py-2 text-center text-xs font-medium ${gate.group ? "border border-emerald-500/50 bg-emerald-500/10 text-emerald-300" : "border border-cyan-500/50 bg-cyan-500/10 text-cyan-300"}`}>
                {gate.group ? "✅ " : "② "}
                {zh ? "加入交流群" : "Group"}
              </a>
            </div>
            {initData ? (
              <>
                <button onClick={onVerify} disabled={verifying} className="mt-2 w-full rounded-xl bg-white/10 py-2 text-sm font-medium text-white ring-1 ring-white/20 disabled:opacity-50">
                  {verifying ? (zh ? "校验中…" : "Checking…") : zh ? "✅ 我已加入，校验解锁" : "✅ I've joined, verify"}
                </button>
                {gate.checked && !(gate.channel && gate.group) && (
                  <p className="mt-2 text-center text-[11px] text-amber-300">
                    {zh ? "还差：" : "Still need: "}
                    {!gate.channel && (zh ? "关注频道 " : "Channel ")}
                    {!gate.group && (zh ? "加入交流群" : "Group")}
                  </p>
                )}
              </>
            ) : (
              <p className="mt-2 text-center text-[11px] text-slate-400">{zh ? "请在 Telegram 内打开本页即可自动校验解锁" : "Open inside Telegram to auto-verify"}</p>
            )}
          </>
        )}
      </div>

      {/* AI closing monthly plans */}
      <SectionTitle icon="💬" title={t.plans.title} sub={t.plans.subtitle} />
      <div className="space-y-2">
        {t.plans.items.map((p) => (
          <div key={p.name} className={`rounded-xl border p-3 ${p.highlight ? "border-cyan-500/60 bg-cyan-500/5" : "border-slate-800 bg-slate-900/40"}`}>
            <div className="flex items-center justify-between">
              <span className="text-sm font-semibold text-white">
                {p.name} {p.highlight && <span className="ml-1 rounded-full bg-cyan-500/20 px-2 py-0.5 text-[10px] text-cyan-300">{t.plans.popular}</span>}
              </span>
              <span className="text-sm font-bold text-cyan-300">
                {p.priceMonthly} <span className="text-[10px] font-normal text-slate-500">{t.plans.perMonth}</span>
              </span>
            </div>
            <div className="mt-0.5 text-[11px] text-slate-500">{p.desc}</div>
            <div className="mt-1.5 flex flex-wrap gap-1">
              {p.features.map((f) => (
                <span key={f} className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-300">{f}</span>
              ))}
            </div>
          </div>
        ))}
      </div>

      {/* deploy one-time */}
      <SectionTitle icon="🛠" title={zh ? "实时换脸 · 一次性部署" : "Live swap · one-time deploy"} />
      <div className="space-y-2">
        {t.realtime.plans.map((p) => (
          <div key={p.name} className="flex items-center justify-between rounded-xl border border-slate-800 bg-slate-900/40 p-3">
            <span className="text-sm font-medium text-white">{p.name}</span>
            <span className="text-sm font-bold text-cyan-300">{p.price}</span>
          </div>
        ))}
      </div>

      {/* all products */}
      <SectionTitle icon="📦" title={zh ? "六大产品挂牌价" : "All products"} sub={t.pricingSection.note} />
      <div className="space-y-2">
        {t.solutions.map((s) => (
          <SolutionCard key={s.id} s={s} />
        ))}
      </div>

      <CtaButton label={zh ? "🚀 领专属折扣码 / 联系报价" : "🚀 Claim code / get a quote"} href={CONTACT_URL} />
    </div>
  );
}

export function EngageView({ t, zh, onContact }: { t: Dict; zh: boolean; onContact: (interest: string) => void }) {
  return (
    <div>
      <SectionTitle icon="🤝" title={t.engage.title} sub={t.engage.subtitle} />
      <div className="space-y-2">
        {t.engage.models.map((m) => (
          <div key={m.id} className={`rounded-2xl border p-3 ${m.highlight ? "border-cyan-500/60 bg-cyan-500/5" : "border-slate-800 bg-slate-900/40"}`}>
            <div className="flex items-center justify-between">
              <span className="text-sm font-bold text-white">{m.name}</span>
              <span className="rounded-full border border-slate-700 px-2 py-0.5 text-[10px] text-cyan-300">{m.badge}</span>
            </div>
            <div className="mt-0.5 text-[11px] text-slate-400">{m.tagline}</div>
            <div className="mt-2 grid grid-cols-2 gap-2 text-[11px]">
              <div className="rounded-lg bg-slate-950/50 p-2">
                <div className="text-slate-500">{t.engage.youLabel}</div>
                <div className="mt-0.5 text-slate-300">{m.you}</div>
              </div>
              <div className="rounded-lg bg-slate-950/50 p-2">
                <div className="text-slate-500">{t.engage.weLabel}</div>
                <div className="mt-0.5 text-slate-300">{m.we}</div>
              </div>
            </div>
            <div className="mt-2 text-sm font-bold text-cyan-300">{m.price}</div>
            <div className="text-[10px] text-slate-500">{m.priceNote}</div>
          </div>
        ))}
      </div>

      {/* matrix */}
      <SectionTitle icon="📊" title={t.engage.matrixTitle} />
      <div className="overflow-hidden rounded-xl border border-slate-800">
        <table className="w-full text-[11px]">
          <thead>
            <tr className="bg-slate-900 text-slate-400">
              <th className="px-2 py-1.5 text-left font-medium"></th>
              {t.engage.matrixCols.map((c) => (
                <th key={c} className="px-1 py-1.5 text-center font-medium">{c}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {t.engage.matrix.map((r, i) => (
              <tr key={r.label} className={i % 2 ? "bg-slate-900/40" : "bg-slate-900/10"}>
                <td className="px-2 py-1.5 text-slate-400">{r.label}</td>
                <td className="px-1 py-1.5 text-center text-slate-300">{r.a}</td>
                <td className="px-1 py-1.5 text-center text-slate-300">{r.b}</td>
                <td className="px-1 py-1.5 text-center text-slate-300">{r.c}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* roi example */}
      <SectionTitle icon="📈" title={t.engage.invest.roiTitle} />
      <div className="space-y-1.5 rounded-xl border border-slate-800 bg-slate-900/40 p-3">
        {t.engage.invest.roiRows.map((r) => (
          <div key={r.label} className="flex items-center justify-between text-[11px]">
            <span className="text-slate-400">{r.label}</span>
            <span className="font-medium text-cyan-300">{r.value}</span>
          </div>
        ))}
        <p className="mt-1 border-t border-slate-800 pt-1.5 text-[10px] leading-relaxed text-slate-500">{t.engage.invest.roiNote}</p>
      </div>

      {/* order steps */}
      <SectionTitle icon="🧾" title={t.orderSteps.title} sub={t.orderSteps.subtitle} />
      <div className="space-y-2">
        {t.orderSteps.steps.map((s, i) => (
          <div key={s.title} className="flex gap-3 rounded-xl border border-slate-800 bg-slate-900/40 p-3">
            <div className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-cyan-500/20 text-xs font-bold text-cyan-300">{i + 1}</div>
            <div>
              <div className="text-sm font-medium text-white">{s.title}</div>
              <div className="mt-0.5 text-[11px] text-slate-400">{s.desc}</div>
            </div>
          </div>
        ))}
      </div>

      <CtaButton label={zh ? "🤝 联系定制顾问" : "🤝 Talk to an advisor"} onClick={() => onContact(zh ? "合作方式咨询" : "Engagement inquiry")} />
    </div>
  );
}

/* ───────────────────────── AI chat + lead (used on home) ───────────────────────── */

export function AiChat({ t, zh }: { t: Dict; zh: boolean }) {
  const [msgs, setMsgs] = useState<{ role: "user" | "assistant"; content: string }[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  async function send() {
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    const next = [...msgs, { role: "user" as const, content: text }];
    setMsgs(next);
    setBusy(true);
    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, lang: zh ? "zh" : "en", history: msgs.slice(-6) }),
      });
      const reply = (await res.text()) || (zh ? "稍后客服联系你～" : "Support will reach you soon.");
      setMsgs([...next, { role: "assistant", content: reply }]);
    } catch {
      setMsgs([...next, { role: "assistant", content: zh ? "网络波动，请重试或点人工客服。" : "Network hiccup, try again." }]);
    } finally {
      setBusy(false);
      setTimeout(() => scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" }), 60);
    }
  }

  return (
    <section id="ai-chat" className="rounded-2xl border border-cyan-700/40 bg-slate-900/60 p-3">
      <div className="mb-2 text-sm font-semibold text-cyan-300">🤖 {zh ? "AI 智能客服 · 直接问" : "AI assistant · ask anything"}</div>
      <div ref={scrollRef} className="max-h-60 space-y-2 overflow-y-auto">
        {msgs.length === 0 && (
          <div className="text-xs text-slate-500">{zh ? "例如：换脸怎么收费？AI 成交能接哪些平台？私有部署多少钱？" : "e.g. How much is face swap? Which platforms? Private deploy price?"}</div>
        )}
        {msgs.map((m, i) => (
          <div key={i} className={m.role === "user" ? "text-right" : "text-left"}>
            <span className={`inline-block max-w-[85%] whitespace-pre-wrap rounded-2xl px-3 py-2 text-sm ${m.role === "user" ? "bg-cyan-500 text-slate-950" : "bg-slate-800 text-slate-100"}`}>{m.content}</span>
          </div>
        ))}
        {busy && <div className="text-left text-xs text-slate-500">{zh ? "AI 正在输入…" : "AI is typing…"}</div>}
      </div>
      <div className="mt-2 flex gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && send()}
          placeholder={zh ? "输入你的问题…" : "Type your question…"}
          className="flex-1 rounded-xl border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-cyan-500"
        />
        <button onClick={send} disabled={busy} className="rounded-xl bg-cyan-500 px-4 py-2 text-sm font-medium text-slate-950 disabled:opacity-50">{zh ? "发送" : "Send"}</button>
      </div>
    </section>
  );
}

export function LeadForm({
  t,
  zh,
  presetInterest,
  view,
  name,
  setName,
  contact,
  setContact,
}: {
  t: Dict;
  zh: boolean;
  presetInterest: string;
  view?: string;
  name: string;
  setName: (v: string) => void;
  contact: string;
  setContact: (v: string) => void;
}) {
  const [msg, setMsg] = useState("");
  const [sending, setSending] = useState(false);

  async function submit() {
    if (!contact.trim()) {
      setMsg(zh ? "请填写联系方式" : "Please enter a contact");
      return;
    }
    setSending(true);
    try {
      const res = await fetch("/api/lead", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name,
          contact,
          interest: presetInterest || (zh ? "小程序留资" : "Mini App lead"),
          source: "miniapp",
          lang: zh ? "zh" : "en",
        }),
      });
      if (res.ok) track("miniapp_lead", { interest: presetInterest || "miniapp", view: view || "home" });
      setMsg(res.ok ? (zh ? "✅ 已提交，客服会尽快联系你！" : "✅ Submitted, we'll contact you soon!") : zh ? "提交失败，请重试" : "Failed, try again");
    } catch {
      setMsg(zh ? "网络错误，请重试" : "Network error, try again");
    } finally {
      setSending(false);
    }
  }

  return (
    <section id="contact" className="rounded-2xl border border-cyan-700/40 bg-slate-900/60 p-3">
      <div className="mb-2 text-sm font-semibold text-cyan-300">📝 {zh ? "留个联系方式 · 客服联系你" : "Leave a contact · we'll reach you"}</div>
      <input value={name} onChange={(e) => setName(e.target.value)} placeholder={zh ? "称呼（选填）" : "Name (optional)"} className="mb-2 w-full rounded-xl border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-cyan-500" />
      <input id="lead-contact" value={contact} onChange={(e) => setContact(e.target.value)} placeholder={zh ? "Telegram / 微信 / 邮箱" : "Telegram / email / etc."} className="mb-2 w-full rounded-xl border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-cyan-500" />
      <button onClick={submit} disabled={sending} className="w-full rounded-xl bg-cyan-500 py-2 text-sm font-semibold text-slate-950 disabled:opacity-50">{sending ? (zh ? "提交中…" : "Submitting…") : zh ? "提交留资" : "Submit"}</button>
      {msg && <div className="mt-2 text-center text-xs text-cyan-300">{msg}</div>}
    </section>
  );
}
