"use client";

import { useEffect, useRef, useState } from "react";
import { content } from "@/lib/content";
import { CHANNEL_URL, GROUP_URL, CONTACT_URL } from "@/lib/site";

type Lang = "zh" | "en";
type Msg = { role: "user" | "assistant"; content: string };

const EMOJI: Record<string, string> = {
  voice: "🎙",
  faceswap: "🎭",
  translate: "💬",
  "private-ai": "🔐",
  "digital-human": "👤",
  "video-dubbing": "🎬",
};

export default function MiniApp() {
  const [lang, setLang] = useState<Lang>("zh");
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [leadName, setLeadName] = useState("");
  const [leadContact, setLeadContact] = useState("");
  const [leadMsg, setLeadMsg] = useState("");
  const [initData, setInitData] = useState("");
  const [gate, setGate] = useState<{ channel: boolean; group: boolean; code: string; checked: boolean }>({
    channel: false,
    group: false,
    code: "",
    checked: false,
  });
  const [verifying, setVerifying] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    try {
      const tg = window.Telegram?.WebApp;
      if (tg) {
        tg.ready();
        tg.expand();
        if (tg.initData) setInitData(tg.initData);
        const u = tg.initDataUnsafe?.user;
        if (u?.first_name) setLeadName(u.first_name);
        if (u?.username) setLeadContact(`@${u.username}`);
        if (u?.language_code && !u.language_code.startsWith("zh")) setLang("en");
      } else if (typeof navigator !== "undefined" && navigator.language.startsWith("en")) {
        setLang("en");
      }
    } catch {
      /* non-fatal */
    }
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [msgs, busy]);

  const t = content[lang];
  const zh = lang === "zh";

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
        body: JSON.stringify({ message: text, lang, history: msgs.slice(-6) }),
      });
      const reply = (await res.text()) || (zh ? "稍后客服联系你～" : "Support will reach you soon.");
      setMsgs([...next, { role: "assistant", content: reply }]);
    } catch {
      setMsgs([...next, { role: "assistant", content: zh ? "网络波动，请重试或点下方人工客服。" : "Network hiccup, try again or tap support." }]);
    } finally {
      setBusy(false);
    }
  }

  async function verifyUnlock() {
    if (!initData || verifying) return;
    setVerifying(true);
    try {
      const res = await fetch("/api/telegram/membership", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ initData }),
      });
      const data = await res.json();
      if (data?.ok) {
        setGate({
          channel: Boolean(data.channel),
          group: Boolean(data.group),
          code: data.code ? String(data.code) : "",
          checked: true,
        });
        try {
          if (data.channel && data.group) window.Telegram?.WebApp?.HapticFeedback?.notificationOccurred?.("success");
        } catch {
          /* ignore */
        }
      }
    } catch {
      /* ignore */
    } finally {
      setVerifying(false);
    }
  }

  async function submitLead() {
    if (!leadContact.trim()) {
      setLeadMsg(zh ? "请填写联系方式" : "Please enter a contact");
      return;
    }
    try {
      const res = await fetch("/api/lead", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: leadName,
          contact: leadContact,
          interest: zh ? "小程序留资" : "Mini App lead",
          source: "miniapp",
          lang,
        }),
      });
      setLeadMsg(res.ok ? (zh ? "✅ 已提交，客服会尽快联系你！" : "✅ Submitted, we'll contact you soon!") : zh ? "提交失败，请重试" : "Failed, try again");
    } catch {
      setLeadMsg(zh ? "网络错误，请重试" : "Network error, try again");
    }
  }

  return (
    <main className="mx-auto min-h-screen max-w-lg bg-slate-950 px-4 pb-10 pt-4 text-slate-100">
      <header className="flex items-center justify-between">
        <div>
          <div className="text-lg font-bold">华灵科技 HuaLing Tech</div>
          <div className="text-[11px] text-cyan-300">{zh ? "华影 LiveAvatar × 灵犀 SoulSync · USDT 结算" : "HuaYing LiveAvatar × LingXi SoulSync · USDT"}</div>
        </div>
        <button
          onClick={() => setLang(zh ? "en" : "zh")}
          className="rounded-lg border border-slate-700 px-2 py-1 text-xs text-slate-300"
        >
          {zh ? "EN" : "中文"}
        </button>
      </header>

      {/* AI chat */}
      <section className="mt-4 rounded-2xl border border-cyan-700/40 bg-slate-900/60 p-3">
        <div className="mb-2 text-sm font-semibold text-cyan-300">🤖 {zh ? "AI 智能客服 · 直接问" : "AI assistant · ask anything"}</div>
        <div ref={scrollRef} className="max-h-60 space-y-2 overflow-y-auto">
          {msgs.length === 0 && (
            <div className="text-xs text-slate-500">
              {zh ? "例如：换脸怎么收费？AI 成交能接哪些平台？私有部署多少钱？" : "e.g. How much is face swap? Which platforms? Private deploy price?"}
            </div>
          )}
          {msgs.map((m, i) => (
            <div key={i} className={m.role === "user" ? "text-right" : "text-left"}>
              <span
                className={`inline-block max-w-[85%] whitespace-pre-wrap rounded-2xl px-3 py-2 text-sm ${
                  m.role === "user" ? "bg-cyan-500 text-slate-950" : "bg-slate-800 text-slate-100"
                }`}
              >
                {m.content}
              </span>
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
          <button onClick={send} disabled={busy} className="rounded-xl bg-cyan-500 px-4 py-2 text-sm font-medium text-slate-950 disabled:opacity-50">
            {zh ? "发送" : "Send"}
          </button>
        </div>
      </section>

      {/* quick actions */}
      <section className="mt-3 grid grid-cols-3 gap-2">
        <a href={CHANNEL_URL} className="rounded-xl border border-slate-700 bg-slate-900/60 px-2 py-3 text-center text-xs">📢 {zh ? "官方频道" : "Channel"}</a>
        <a href={GROUP_URL} className="rounded-xl border border-slate-700 bg-slate-900/60 px-2 py-3 text-center text-xs">💬 {zh ? "交流群" : "Group"}</a>
        <a href={CONTACT_URL} className="rounded-xl border border-slate-700 bg-slate-900/60 px-2 py-3 text-center text-xs">👤 {zh ? "人工客服" : "Support"}</a>
      </section>

      {/* unlock gate: join channel + group -> one-time discount code */}
      <section id="unlock" className="mt-3 rounded-2xl border border-violet-500/40 bg-gradient-to-br from-violet-500/10 to-cyan-500/10 p-3">
        {gate.channel && gate.group && gate.code ? (
          <div className="text-center">
            <div className="text-sm font-semibold text-violet-200">🎉 {zh ? "已解锁专属优惠" : "Offer unlocked"}</div>
            <div className="mx-auto mt-2 inline-flex flex-col items-center rounded-xl border border-cyan-400/40 bg-slate-950/70 px-6 py-3">
              <span className="text-[11px] text-slate-400">{zh ? "你的专属一次性折扣码" : "Your one-time code"}</span>
              <span className="font-mono text-2xl font-bold tracking-widest text-cyan-300">{gate.code}</span>
            </div>
            <p className="mt-2 text-[11px] text-slate-400">{zh ? "把折扣码发给客服即可享专属价（仅限一次）" : "Send this code to support for your exclusive price (one-time)"}</p>
            <a href={CONTACT_URL} className="mt-3 inline-block rounded-xl bg-gradient-to-r from-cyan-400 to-violet-500 px-6 py-2 text-sm font-semibold text-slate-950">
              {zh ? "🚀 联系客服领取" : "🚀 Claim with support"}
            </a>
          </div>
        ) : (
          <>
            <div className="text-sm font-semibold text-violet-200">🔓 {zh ? "关注频道 + 进群，解锁专属折扣码" : "Join channel + group to unlock a code"}</div>
            <div className="mt-2 grid grid-cols-2 gap-2">
              <a
                href={CHANNEL_URL}
                className={`rounded-xl px-2 py-2 text-center text-xs font-medium ${gate.channel ? "border border-emerald-500/50 bg-emerald-500/10 text-emerald-300" : "bg-cyan-500 text-slate-950"}`}
              >
                {gate.channel ? "✅ " : "① "}
                {zh ? "关注频道" : "Channel"}
              </a>
              <a
                href={GROUP_URL}
                className={`rounded-xl px-2 py-2 text-center text-xs font-medium ${gate.group ? "border border-emerald-500/50 bg-emerald-500/10 text-emerald-300" : "border border-cyan-500/50 bg-cyan-500/10 text-cyan-300"}`}
              >
                {gate.group ? "✅ " : "② "}
                {zh ? "加入交流群" : "Group"}
              </a>
            </div>
            {initData ? (
              <>
                <button
                  onClick={verifyUnlock}
                  disabled={verifying}
                  className="mt-2 w-full rounded-xl bg-white/10 py-2 text-sm font-medium text-white ring-1 ring-white/20 disabled:opacity-50"
                >
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
              <p className="mt-2 text-center text-[11px] text-slate-400">
                {zh ? "请在 Telegram 内打开本页即可自动校验解锁" : "Open this page inside Telegram to auto-verify"}
              </p>
            )}
          </>
        )}
      </section>

      {/* products */}
      <section id="pricing" className="mt-4">
        <div className="mb-2 text-sm font-semibold">📦 {zh ? "六大产品 · 价格" : "Products & pricing"}</div>
        <div className="space-y-2">
          {t.solutions.map((s) => (
            <div key={s.id} className="rounded-xl border border-slate-800 bg-slate-900/40 p-3">
              <div className="text-sm font-semibold">
                {EMOJI[s.id] ?? "📦"} {s.title}
              </div>
              <div className="mt-0.5 text-xs text-slate-400">{s.desc}</div>
              <div className="mt-1 flex flex-wrap gap-1">
                {s.pricing.slice(0, 3).map((p, i) => (
                  <span key={i} className="rounded-md bg-slate-800 px-2 py-0.5 text-[11px] text-cyan-300">
                    {p.plan} {p.price}
                  </span>
                ))}
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* lead form */}
      <section id="contact" className="mt-4 rounded-2xl border border-cyan-700/40 bg-slate-900/60 p-3">
        <div className="mb-2 text-sm font-semibold text-cyan-300">📝 {zh ? "留个联系方式 · 客服联系你" : "Leave a contact · we'll reach you"}</div>
        <input
          value={leadName}
          onChange={(e) => setLeadName(e.target.value)}
          placeholder={zh ? "称呼（选填）" : "Name (optional)"}
          className="mb-2 w-full rounded-xl border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-cyan-500"
        />
        <input
          id="lead-contact"
          value={leadContact}
          onChange={(e) => setLeadContact(e.target.value)}
          placeholder={zh ? "Telegram / 微信 / 邮箱" : "Telegram / email / etc."}
          className="mb-2 w-full rounded-xl border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-cyan-500"
        />
        <button onClick={submitLead} className="w-full rounded-xl bg-cyan-500 py-2 text-sm font-semibold text-slate-950">
          {zh ? "提交留资" : "Submit"}
        </button>
        {leadMsg && <div className="mt-2 text-center text-xs text-cyan-300">{leadMsg}</div>}
      </section>

      <footer className="mt-5 text-center text-[11px] text-slate-600">
        {zh ? "数据私有不出网 · 全程 USDT 结算 · 支持私有定制" : "Private & local · USDT only · fully customizable"}
      </footer>
    </main>
  );
}
