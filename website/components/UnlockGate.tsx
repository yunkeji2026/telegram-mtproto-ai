"use client";

import { useState } from "react";
import { motion } from "framer-motion";
import { Lock, Check, Send, Users, Sparkles } from "lucide-react";
import { useLang } from "./LanguageContext";
import { useTelegram } from "./TelegramProvider";
import { CHANNEL_URL, GROUP_URL, CONTACT_URL } from "@/lib/site";
import { track } from "@/lib/track";
import Reveal from "./fx/Reveal";

export default function UnlockGate() {
  const { t } = useLang();
  const g = t.gate;
  const { isMiniApp, initData } = useTelegram();

  const [channel, setChannel] = useState(false);
  const [group, setGroup] = useState(false);
  const [checking, setChecking] = useState(false);
  const [checked, setChecked] = useState(false);
  const [code, setCode] = useState("");

  const unlocked = channel && group;

  async function verify() {
    if (!initData) return;
    setChecking(true);
    try {
      const res = await fetch("/api/telegram/membership", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ initData }),
      });
      const data = await res.json();
      if (data.ok) {
        setChannel(Boolean(data.channel));
        setGroup(Boolean(data.group));
        setChecked(true);
        if (data.code) setCode(String(data.code));
        if (data.channel && data.group) track("cta_click", { where: "gate_unlocked" });
      }
    } catch {
      /* ignore */
    } finally {
      setChecking(false);
    }
  }

  return (
    <section id="unlock" className="relative py-16">
      <div className="mx-auto max-w-3xl px-5">
        <Reveal>
          <div className="relative overflow-hidden rounded-3xl border border-neon-violet/30 bg-gradient-to-br from-neon-violet/10 via-ink-900/70 to-neon-cyan/10 p-8 md:p-10">
            <div className="pointer-events-none absolute -right-20 -top-20 h-56 w-56 rounded-full bg-neon-violet/15 blur-3xl" />

            <div className="relative text-center">
              <span className="inline-flex items-center gap-1.5 rounded-full border border-neon-violet/40 bg-neon-violet/10 px-3 py-1 text-xs font-medium text-neon-violet">
                {unlocked ? <Sparkles className="h-3.5 w-3.5" /> : <Lock className="h-3.5 w-3.5" />}
                {g.badge}
              </span>

              {!unlocked ? (
                <>
                  <h2 className="mt-4 text-2xl font-bold text-white md:text-3xl">{g.title}</h2>
                  <p className="mx-auto mt-3 max-w-xl text-slate-300">{g.subtitle}</p>

                  <div className="mt-7 flex flex-col gap-3 sm:flex-row sm:justify-center">
                    <a
                      href={CHANNEL_URL}
                      target="_blank"
                      rel="noreferrer"
                      onClick={() => track("cta_click", { where: "gate_channel" })}
                      className={`inline-flex items-center justify-center gap-2 rounded-full px-6 py-3 text-sm font-semibold ${channel ? "border border-emerald-500/50 bg-emerald-500/10 text-emerald-300" : "bg-gradient-to-r from-neon-cyan to-neon-violet text-ink-950"}`}
                    >
                      {channel ? <Check className="h-4 w-4" /> : <Send className="h-4 w-4" />}
                      {channel ? g.joinedChannel : g.joinChannel}
                    </a>
                    <a
                      href={GROUP_URL}
                      target="_blank"
                      rel="noreferrer"
                      onClick={() => track("cta_click", { where: "gate_group" })}
                      className={`inline-flex items-center justify-center gap-2 rounded-full px-6 py-3 text-sm font-semibold ${group ? "border border-emerald-500/50 bg-emerald-500/10 text-emerald-300" : "border border-neon-cyan/40 bg-neon-cyan/10 text-neon-cyan"}`}
                    >
                      {group ? <Check className="h-4 w-4" /> : <Users className="h-4 w-4" />}
                      {group ? g.joinedGroup : g.joinGroup}
                    </a>
                  </div>

                  {isMiniApp ? (
                    <div className="mt-6">
                      <motion.button
                        whileTap={{ scale: 0.97 }}
                        onClick={verify}
                        disabled={checking}
                        className="inline-flex items-center gap-2 rounded-full bg-white/10 px-6 py-2.5 text-sm font-medium text-white ring-1 ring-white/20 disabled:opacity-50"
                      >
                        {checking ? g.checking : g.verify}
                      </motion.button>
                      {checked && !unlocked && (
                        <p className="mt-3 text-xs text-amber-300">{g.notYet}</p>
                      )}
                    </div>
                  ) : (
                    <p className="mt-6 text-xs text-slate-400">{g.webNote}</p>
                  )}
                </>
              ) : (
                <>
                  <h2 className="mt-4 text-2xl font-bold text-white md:text-3xl">{g.unlockedTitle}</h2>
                  <p className="mx-auto mt-3 max-w-xl text-slate-300">{g.unlockedDesc}</p>
                  <div className="mx-auto mt-6 inline-flex flex-col items-center gap-1 rounded-2xl border border-neon-cyan/30 bg-ink-950/60 px-8 py-4">
                    <span className="text-xs text-slate-400">{g.codeLabel}</span>
                    <span className="font-mono text-2xl font-bold tracking-widest text-neon-cyan">{code || g.code}</span>
                  </div>
                  <div className="mt-6">
                    <a
                      href={CONTACT_URL}
                      target="_blank"
                      rel="noreferrer"
                      onClick={() => track("cta_click", { where: "gate_contact" })}
                      className="inline-flex items-center justify-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-7 py-3.5 text-sm font-semibold text-ink-950"
                    >
                      <Sparkles className="h-4 w-4" />
                      {g.cta}
                    </a>
                  </div>
                </>
              )}
            </div>
          </div>
        </Reveal>
      </div>
    </section>
  );
}
