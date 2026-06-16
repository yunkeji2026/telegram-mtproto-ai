"use client";

import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { MessageSquare, X, Send, Bot, Sparkles, CheckCircle2, Globe } from "lucide-react";
import { useLang } from "./LanguageContext";
import { useTelegram } from "./TelegramProvider";
import { cleanMarkdown } from "@/lib/clean-markdown";
import { CONTACT_URL } from "@/lib/site";
import { track } from "@/lib/track";
import { detectLang } from "@/lib/detect-lang";
import { getSession, setSession } from "@/lib/safe-storage";

type Msg = { role: "user" | "assistant"; content: string };

const INTENT = /价格|多少钱|报价|购买|下单|怎么收费|套餐|合作|定制|price|cost|buy|order|quote|pricing|plan|deploy/i;

const COPY = {
  zh: {
    title: "AI 在线客服",
    sub: "由 AI 自动成交聊天系统驱动",
    greet: "你好 👋 我是无界科技 BOUNDLESS 的 AI 客服。换脸、克隆声音、直播换脸换声、实时换语言、AI 自动成交、私有部署、价格——都可以问我。\n（支持任意语言：用你客户的母语问我试试 🌍）",
    placeholder: "任意语言输入你的问题…",
    suggestions: ["AI 自动成交怎么收费？", "实时换脸支持视频通话吗？", "¿Cuánto cuesta el chat con IA?"],
    leave: "留个联系方式，让客服联系我",
    disclaimer: "AI 回答仅供参考，详情以客服确认为准。",
    error: "网络繁忙，请稍后重试或点下方留资。",
    leadPrompt: "想要专属方案 / 报价？留个联系方式，客服 5 分钟内联系你 👇",
    contactPh: "Telegram / WhatsApp / 邮箱",
    leadSubmit: "提交",
    leadOk: "已收到，马上联系你 ✅",
    teaser: "在找出海获客方案？问我 AI 自动成交怎么帮你多赚 👋",
    human: "转人工客服",
    replyIn: "AI 将用此语言实时回复",
  },
  en: {
    title: "AI Live Support",
    sub: "Powered by our AI auto-closing chat",
    greet: "Hi 👋 I'm BOUNDLESS's AI agent. Ask me about face swap, voice cloning, live face/voice swap, live translation, AI auto-closing, private deployment, pricing.\n(Any language works — try your customer's native tongue 🌍)",
    placeholder: "Type in any language…",
    suggestions: ["How is AI closing priced?", "Does live swap work on video calls?", "¿Cuánto cuesta el chat con IA?"],
    leave: "Leave my contact for support",
    disclaimer: "AI answers are for reference; confirm details with support.",
    error: "Busy now, please retry later or leave your contact below.",
    leadPrompt: "Want a tailored plan / quote? Leave your contact and we'll reach you in ~5 min 👇",
    contactPh: "Telegram / WhatsApp / email",
    leadSubmit: "Submit",
    leadOk: "Got it — reaching out shortly ✅",
    teaser: "Scaling cross-border sales? Ask how AI auto-closing earns you more 👋",
    human: "Talk to a human",
    replyIn: "AI replies live in this language",
  },
};

export default function AIChat() {
  const { lang } = useLang();
  const { isMiniApp } = useTelegram();
  const c = COPY[lang];

  const [open, setOpen] = useState(false);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [userTurns, setUserTurns] = useState(0);
  const [showLead, setShowLead] = useState(false);
  const [leadContact, setLeadContact] = useState("");
  const [leadDone, setLeadDone] = useState(false);
  const [teaser, setTeaser] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [msgs, busy, showLead]);

  // proactive greeting: once per session, only on web, after dwell
  useEffect(() => {
    if (isMiniApp) return;
    if (typeof window === "undefined") return;
    if (getSession("yt-teaser") === "1") return;
    const id = setTimeout(() => {
      if (!open) {
        setTeaser(true);
        track("ai_chat_teaser");
      }
    }, 18000);
    return () => clearTimeout(id);
  }, [isMiniApp, open]);

  function dismissTeaser() {
    setTeaser(false);
    setSession("yt-teaser", "1");
  }

  async function send(text: string) {
    const q = text.trim();
    if (!q || busy) return;
    setInput("");
    const baseHistory = msgs.slice(-6);
    const next: Msg[] = [...msgs, { role: "user", content: q }];
    setMsgs(next);
    setBusy(true);
    setStreaming(true);
    track("ai_chat", { len: q.length });

    const turns = userTurns + 1;
    setUserTurns(turns);
    const intent = INTENT.test(q);

    // add empty assistant message to stream into
    setMsgs((m) => [...m, { role: "assistant", content: "" }]);

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: q, lang, history: baseHistory }),
      });
      if (!res.ok || !res.body) throw new Error("bad");
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let acc = "";
      setStreaming(false); // first byte → stop the "thinking" dots
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        acc += decoder.decode(value, { stream: true });
        const display = acc;
        setMsgs((m) => {
          const copy = [...m];
          copy[copy.length - 1] = { role: "assistant", content: display };
          return copy;
        });
      }
    } catch {
      setMsgs((m) => {
        const copy = [...m];
        copy[copy.length - 1] = { role: "assistant", content: c.error };
        return copy;
      });
    } finally {
      setBusy(false);
      setStreaming(false);
      // conversation → lead: trigger on buy-intent or after 2 turns
      if (!leadDone && (intent || turns >= 2)) {
        setShowLead(true);
        track("ai_chat_lead_prompt", { intent, turns });
      }
    }
  }

  async function submitLead() {
    const contact = leadContact.trim();
    if (!contact) return;
    const recent = msgs.filter((m) => m.role === "user").slice(-3).map((m) => m.content).join(" | ");
    try {
      await fetch("/api/lead", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          contact,
          interest: lang === "zh" ? "AI 在线咨询" : "AI live chat",
          message: recent,
          lang,
          source: "ai_chat",
          path: typeof window !== "undefined" ? window.location.pathname : "",
        }),
      });
      setLeadDone(true);
      setShowLead(false);
      track("lead_submit", { source: "ai_chat" });
    } catch {
      /* keep form open on failure */
    }
  }

  function goLead() {
    setOpen(false);
    const el = document.getElementById("contact");
    el?.scrollIntoView({ behavior: "smooth", block: "start" });
    setTimeout(() => document.querySelector<HTMLInputElement>("#lead-contact")?.focus(), 500);
  }

  // hide on mobile inside mini app to avoid covering TG MainButton
  const hideLauncher = isMiniApp;

  return (
    <>
      {/* proactive teaser */}
      <AnimatePresence>
        {teaser && !open && !hideLauncher && (
          <motion.div
            initial={{ opacity: 0, y: 12, scale: 0.95 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 12, scale: 0.95 }}
            className="fixed bottom-36 right-4 z-50 w-[min(260px,calc(100vw-2rem))] rounded-2xl rounded-br-sm border border-neon-cyan/30 bg-ink-900/95 p-3 shadow-2xl backdrop-blur lg:bottom-20 lg:right-5"
          >
            <button
              onClick={dismissTeaser}
              aria-label="dismiss"
              className="absolute -right-1.5 -top-1.5 grid h-5 w-5 place-items-center rounded-full bg-ink-800 text-slate-400 ring-1 ring-white/10 hover:text-white"
            >
              <X className="h-3 w-3" />
            </button>
            <button
              onClick={() => {
                setOpen(true);
                dismissTeaser();
                track("ai_chat_open", { from: "teaser" });
              }}
              className="flex items-start gap-2 text-left"
            >
              <span className="grid h-7 w-7 shrink-0 place-items-center rounded-full bg-gradient-to-br from-neon-cyan to-neon-violet text-ink-950">
                <Bot className="h-4 w-4" />
              </span>
              <span className="text-xs leading-relaxed text-slate-200">{c.teaser}</span>
            </button>
          </motion.div>
        )}
      </AnimatePresence>

      {!hideLauncher && (
        <button
          onClick={() => {
            setOpen((v) => !v);
            dismissTeaser();
            track("ai_chat_open");
          }}
          aria-label="AI chat"
          className="fixed bottom-20 right-4 z-50 grid h-14 w-14 place-items-center rounded-full bg-gradient-to-br from-neon-cyan to-neon-violet text-ink-950 shadow-lg shadow-neon-cyan/30 transition hover:scale-105 lg:bottom-5 lg:right-5"
        >
          {open ? <X className="h-6 w-6" /> : <MessageSquare className="h-6 w-6" />}
          {!open && (
            <span className="absolute -right-0.5 -top-0.5 h-3.5 w-3.5 animate-pulse rounded-full bg-emerald-400 ring-2 ring-ink-950" />
          )}
        </button>
      )}

      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ opacity: 0, y: 24, scale: 0.96 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 24, scale: 0.96 }}
            transition={{ duration: 0.2 }}
            className="fixed bottom-36 right-4 z-50 flex h-[min(520px,64vh)] w-[min(380px,calc(100vw-2rem))] flex-col overflow-hidden rounded-2xl border border-white/10 bg-ink-900/95 shadow-2xl backdrop-blur lg:bottom-24 lg:right-5"
          >
            {/* header */}
            <div className="flex items-center gap-3 border-b border-white/10 bg-gradient-to-r from-neon-cyan/15 to-neon-violet/15 px-4 py-3">
              <span className="grid h-9 w-9 place-items-center rounded-full bg-gradient-to-br from-neon-cyan to-neon-violet text-ink-950">
                <Bot className="h-5 w-5" />
              </span>
              <div className="flex-1">
                <div className="text-sm font-semibold text-white">{c.title}</div>
                <div className="flex items-center gap-1 text-[11px] text-emerald-300">
                  <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
                  {c.sub}
                </div>
              </div>
              <button onClick={() => setOpen(false)} aria-label="close" className="text-slate-400 hover:text-white">
                <X className="h-5 w-5" />
              </button>
            </div>

            {/* messages */}
            <div ref={scrollRef} className="flex-1 space-y-3 overflow-y-auto p-4">
              <Bubble role="assistant">{c.greet}</Bubble>

              {msgs.length === 0 && (
                <div className="space-y-2 pt-1">
                  {c.suggestions.map((s) => (
                    <button
                      key={s}
                      onClick={() => send(s)}
                      className="block w-full rounded-xl border border-white/10 bg-white/[0.03] px-3 py-2 text-left text-xs text-slate-300 transition hover:border-neon-cyan/40 hover:text-white"
                    >
                      <Sparkles className="mr-1.5 inline h-3 w-3 text-neon-cyan" />
                      {s}
                    </button>
                  ))}
                </div>
              )}

              {msgs.map((m, i) => {
                if (m.role === "assistant" && !m.content) return null;
                return (
                  <Bubble key={i} role={m.role}>
                    {m.role === "assistant" ? cleanMarkdown(m.content) : m.content}
                  </Bubble>
                );
              })}

              {streaming && (
                <div className="mr-auto flex items-center gap-1.5 rounded-2xl rounded-tl-sm border border-white/10 bg-ink-800/80 px-3 py-2.5">
                  {[0, 1, 2].map((i) => (
                    <motion.span
                      key={i}
                      className="h-1.5 w-1.5 rounded-full bg-neon-cyan"
                      animate={{ opacity: [0.3, 1, 0.3] }}
                      transition={{ duration: 0.9, repeat: Infinity, delay: i * 0.18 }}
                    />
                  ))}
                </div>
              )}

              {/* conversation → lead capture */}
              <AnimatePresence>
                {showLead && !leadDone && (
                  <motion.div
                    initial={{ opacity: 0, y: 10 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0 }}
                    className="rounded-2xl border border-neon-cyan/30 bg-neon-cyan/[0.06] p-3"
                  >
                    <p className="text-xs text-slate-200">{c.leadPrompt}</p>
                    <form
                      onSubmit={(e) => {
                        e.preventDefault();
                        void submitLead();
                      }}
                      className="mt-2 flex gap-2"
                    >
                      <input
                        value={leadContact}
                        onChange={(e) => setLeadContact(e.target.value)}
                        placeholder={c.contactPh}
                        maxLength={200}
                        className="flex-1 rounded-lg border border-white/10 bg-ink-950/60 px-3 py-2 text-xs text-white placeholder:text-slate-500 outline-none focus:border-neon-cyan/50"
                      />
                      <button
                        type="submit"
                        disabled={!leadContact.trim()}
                        className="rounded-lg bg-gradient-to-r from-neon-cyan to-neon-violet px-3 py-2 text-xs font-semibold text-ink-950 disabled:opacity-50"
                      >
                        {c.leadSubmit}
                      </button>
                    </form>
                  </motion.div>
                )}
              </AnimatePresence>

              {leadDone && (
                <div className="flex items-center justify-center gap-1.5 rounded-xl border border-emerald-400/30 bg-emerald-400/10 px-3 py-2 text-xs text-emerald-300">
                  <CheckCircle2 className="h-4 w-4" />
                  {c.leadOk}
                </div>
              )}
            </div>

            {/* footer */}
            <div className="border-t border-white/10 p-3">
              <div className="mb-2 flex gap-2">
                <button
                  onClick={goLead}
                  className="flex-1 rounded-lg border border-neon-cyan/30 bg-neon-cyan/5 py-1.5 text-xs font-medium text-neon-cyan transition hover:bg-neon-cyan/10"
                >
                  {c.leave}
                </button>
                <a
                  href={CONTACT_URL}
                  target="_blank"
                  rel="noreferrer"
                  onClick={() => track("cta_click", { where: "ai_chat_human" })}
                  className="rounded-lg border border-white/10 px-3 py-1.5 text-xs font-medium text-slate-300 transition hover:border-white/30 hover:text-white"
                >
                  {c.human}
                </a>
              </div>
              {input.trim() &&
                (() => {
                  const d = detectLang(input);
                  return d.code ? (
                    <div className="mb-1.5 flex items-center gap-1.5 text-[10px] font-medium text-neon-cyan">
                      <Globe className="h-3 w-3" />
                      <span>
                        {d.native} · {c.replyIn}
                      </span>
                    </div>
                  ) : null;
                })()}
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  send(input);
                }}
                className="flex items-center gap-2"
              >
                <input
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  placeholder={c.placeholder}
                  maxLength={1000}
                  className="flex-1 rounded-full border border-white/10 bg-ink-950/60 px-4 py-2.5 text-sm text-white placeholder:text-slate-500 outline-none focus:border-neon-cyan/50"
                />
                <button
                  type="submit"
                  disabled={busy || !input.trim()}
                  aria-label="send"
                  className="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-gradient-to-br from-neon-cyan to-neon-violet text-ink-950 disabled:opacity-50"
                >
                  <Send className="h-4 w-4" />
                </button>
              </form>
              <p className="mt-1.5 text-center text-[10px] text-slate-500">{c.disclaimer}</p>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </>
  );
}

function Bubble({ role, children }: { role: "user" | "assistant"; children: React.ReactNode }) {
  const out = role === "user";
  return (
    <div
      className={`max-w-[88%] whitespace-pre-wrap rounded-2xl px-3.5 py-2.5 text-sm ${
        out
          ? "ml-auto rounded-tr-sm bg-gradient-to-r from-neon-cyan to-neon-violet text-ink-950"
          : "mr-auto rounded-tl-sm border border-white/10 bg-ink-800/80 text-slate-100"
      }`}
    >
      {children}
    </div>
  );
}
