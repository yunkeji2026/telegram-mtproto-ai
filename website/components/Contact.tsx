"use client";

import { useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { Send, Wallet, ShieldAlert, ShieldCheck, Copy, Check } from "lucide-react";
import { QRCodeSVG } from "qrcode.react";
import { useLang } from "./LanguageContext";
import { useTelegram } from "./TelegramProvider";
import { CONTACT_URL, BOT_URL } from "@/lib/site";
import { track } from "@/lib/track";
import LeadForm from "./LeadForm";

export default function Contact() {
  const { t, lang } = useLang();
  const { isMiniApp } = useTelegram();
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(t.contact.telegramHandle);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      /* clipboard unavailable */
    }
  };

  return (
    <section id="contact" className="relative py-24">
      <div className="pointer-events-none absolute left-1/2 top-0 h-80 w-[600px] -translate-x-1/2 rounded-full bg-neon-violet/15 blur-[140px]" />
      <div className="relative mx-auto max-w-5xl px-5">
        <motion.div
          initial={{ opacity: 0, y: 24 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ duration: 0.5 }}
          className="glass rounded-3xl p-8 md:p-12"
        >
          <div className="text-center">
            <h2 className="text-3xl font-bold text-white md:text-4xl">{t.contact.title}</h2>
            <p className="mx-auto mt-3 max-w-xl text-slate-400">{t.contact.subtitle}</p>
          </div>

          <div className="mt-10 grid gap-5 md:grid-cols-3">
            <div className="flex flex-col rounded-2xl border border-white/10 bg-ink-900/60 p-6">
              <span className="grid h-11 w-11 place-items-center rounded-xl bg-neon-blue/20 text-neon-cyan">
                <Send className="h-5 w-5" />
              </span>
              <h3 className="mt-4 font-semibold text-white">{t.contact.telegram}</h3>
              <button
                onClick={handleCopy}
                className="group mt-1 inline-flex items-center gap-2 text-sm text-slate-400 transition hover:text-white"
              >
                {t.contact.telegramHandle}
                {copied ? (
                  <Check className="h-3.5 w-3.5 text-neon-cyan" />
                ) : (
                  <Copy className="h-3.5 w-3.5 opacity-60 group-hover:opacity-100" />
                )}
              </button>
              <a
                href={CONTACT_URL}
                target="_blank"
                rel="noreferrer"
                onClick={() => track("cta_click", { where: "contact" })}
                className="mt-4 inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-5 py-2.5 text-sm font-medium text-ink-950 transition hover:opacity-90"
              >
                {t.contact.cta}
              </a>
              {!isMiniApp && (
                <a
                  href={BOT_URL}
                  target="_blank"
                  rel="noreferrer"
                  onClick={() => track("cta_click", { where: "contact_bot" })}
                  className="mt-2 inline-flex items-center gap-2 rounded-full border border-neon-cyan/30 bg-neon-cyan/5 px-5 py-2.5 text-sm font-medium text-neon-cyan transition hover:bg-neon-cyan/10"
                >
                  {lang === "zh" ? "🤖 打开机器人 / Mini App" : "🤖 Open Bot / Mini App"}
                </a>
              )}
              <div className="mt-5 flex items-center gap-3">
                <span className="rounded-lg bg-white p-1.5">
                  <QRCodeSVG value={CONTACT_URL} size={72} level="M" />
                </span>
                <span className="text-xs text-slate-400">{t.contact.scanHint}</span>
              </div>
            </div>

            <div className="rounded-2xl border border-white/10 bg-ink-900/60 p-6">
              <span className="grid h-11 w-11 place-items-center rounded-xl bg-neon-violet/20 text-neon-violet">
                <Wallet className="h-5 w-5" />
              </span>
              <h3 className="mt-4 font-semibold text-white">{t.contact.usdt}</h3>
              <p className="mt-1 text-sm text-slate-400">{t.contact.networks}</p>
              <p className="mt-3 inline-flex items-center gap-1.5 rounded-full border border-emerald-400/30 bg-emerald-400/10 px-2.5 py-1 text-[11px] font-medium text-emerald-300">
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400" />
                {t.contact.responseTime}
              </p>
              <p className="mt-3 flex items-start gap-1.5 text-xs text-amber-300/80">
                <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                {t.contact.usdtNote}
              </p>
            </div>

            <div className="rounded-2xl border border-white/10 bg-ink-900/60 p-6">
              <span className="grid h-11 w-11 place-items-center rounded-xl bg-emerald-400/15 text-emerald-300">
                <ShieldCheck className="h-5 w-5" />
              </span>
              <h3 className="mt-4 font-semibold text-white">{t.contact.compliance}</h3>
              <p className="mt-1 text-sm leading-relaxed text-slate-400">{t.contact.complianceNote}</p>
            </div>
          </div>

          <LeadForm />
        </motion.div>
      </div>

      <AnimatePresence>
        {copied && (
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 20 }}
            className="glass fixed bottom-6 left-1/2 z-50 -translate-x-1/2 rounded-full px-5 py-2.5 text-sm text-white"
          >
            <span className="inline-flex items-center gap-2">
              <Check className="h-4 w-4 text-neon-cyan" />
              {lang === "zh" ? "已复制客服账号" : "Support handle copied"}
            </span>
          </motion.div>
        )}
      </AnimatePresence>
    </section>
  );
}
