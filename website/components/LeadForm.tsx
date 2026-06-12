"use client";

import { useState, useEffect } from "react";
import { useTelegram } from "./TelegramProvider";
import { AnimatePresence, motion } from "framer-motion";
import { Send, CheckCircle2, Loader2 } from "lucide-react";
import { useLang } from "./LanguageContext";
import { track } from "@/lib/track";

type Status = "idle" | "sending" | "ok" | "error";

/**
 * Light, permissive contact validation. Accepts the channels we actually use:
 * email, @handle, t.me link, phone (>=6 digits), WeChat/WhatsApp ids.
 * Goal is to block obvious junk ("asdf", "123") without rejecting real contacts.
 */
export function isValidContact(raw: string): boolean {
  const v = raw.trim();
  if (v.length < 4) return false;
  const isEmail = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v);
  const isHandle = /^@?[a-zA-Z0-9_]{4,}$/.test(v);
  const isTme = /(?:t\.me\/|telegram\.me\/|wa\.me\/)/i.test(v);
  const digits = (v.match(/\d/g) || []).length;
  const isPhone = digits >= 6 && /^[+\d()\-\s]+$/.test(v);
  const hasLetters = /[a-zA-Z\u4e00-\u9fa5]/.test(v);
  // fallback: reasonably long alphanumeric id (WeChat/WhatsApp display names etc.)
  const isId = v.length >= 5 && hasLetters;
  return isEmail || isHandle || isTme || isPhone || isId;
}

export default function LeadForm() {
  const { t, lang } = useLang();
  const { isMiniApp, user, initData } = useTelegram();
  const f = t.lead;

  const [name, setName] = useState("");
  const [contact, setContact] = useState("");
  const [interest, setInterest] = useState(f.interests[0]);
  const [message, setMessage] = useState("");
  const [hp, setHp] = useState("");
  const [status, setStatus] = useState<Status>("idle");
  const [touched, setTouched] = useState(false);

  const contactOk = isValidContact(contact);
  const showContactErr = touched && contact.trim().length > 0 && !contactOk;

  useEffect(() => {
    if (!user) return;
    setName((n) => n || user.first_name || "");
    setContact((c) => {
      if (c) return c;
      if (user.username) return `@${user.username}`;
      return `tg:${user.id}`;
    });
  }, [user]);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (status === "sending") return;
    setTouched(true);
    if (!isValidContact(contact)) return;
    setStatus("sending");
    try {
      const res = await fetch("/api/lead", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name,
          contact,
          interest,
          message,
          hp,
          lang,
          path: typeof window !== "undefined" ? window.location.pathname : "",
          source: isMiniApp ? "miniapp" : "web",
          tg_user_id: user?.id ? String(user.id) : "",
          initData: isMiniApp ? initData : "",
        }),
      });
      if (!res.ok) throw new Error("bad");
      setStatus("ok");
      track("lead_submit", { interest, source: isMiniApp ? "miniapp" : "web" });
    } catch {
      setStatus("error");
    }
  }

  const inputCls =
    "w-full rounded-xl border border-white/10 bg-ink-950/50 px-4 py-2.5 text-sm text-white placeholder:text-slate-500 outline-none transition focus:border-neon-cyan/50 focus:ring-1 focus:ring-neon-cyan/30";

  return (
    <div className="mt-6 rounded-2xl border border-white/10 bg-ink-900/40 p-6 md:p-8">
      <h3 className="text-lg font-semibold text-white">{f.title}</h3>
      <p className="mt-1 text-sm text-slate-400">{f.subtitle}</p>

      <AnimatePresence mode="wait">
        {status === "ok" ? (
          <motion.div
            key="ok"
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            className="mt-6 flex flex-col items-center gap-2 rounded-xl border border-emerald-400/30 bg-emerald-400/10 px-6 py-8 text-center"
          >
            <CheckCircle2 className="h-9 w-9 text-emerald-400" />
            <p className="text-base font-semibold text-white">{f.successTitle}</p>
            <p className="max-w-md text-sm text-slate-300">{f.successDesc}</p>
          </motion.div>
        ) : (
          <motion.form
            key="form"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            onSubmit={onSubmit}
            className="mt-5 grid gap-4 md:grid-cols-2"
          >
            {/* honeypot */}
            <input
              type="text"
              tabIndex={-1}
              autoComplete="off"
              value={hp}
              onChange={(e) => setHp(e.target.value)}
              className="hidden"
              aria-hidden="true"
            />

            <div>
              <label className="mb-1.5 block text-xs text-slate-400">{f.name}</label>
              <input className={inputCls} value={name} onChange={(e) => setName(e.target.value)} placeholder={f.namePh} maxLength={80} />
            </div>

            <div>
              <label className="mb-1.5 block text-xs text-slate-400">
                {f.contact} <span className="text-neon-cyan">*</span>
              </label>
              <input
                id="lead-contact"
                className={`${inputCls} ${showContactErr ? "border-rose-400/60 focus:border-rose-400/60 focus:ring-rose-400/30" : ""}`}
                value={contact}
                onChange={(e) => setContact(e.target.value)}
                onBlur={() => setTouched(true)}
                placeholder={f.contactPh}
                maxLength={200}
                required
                aria-invalid={showContactErr}
                aria-describedby={showContactErr ? "lead-contact-err" : undefined}
              />
              {showContactErr && (
                <p id="lead-contact-err" className="mt-1.5 text-[11px] text-rose-400">
                  {f.contactInvalid}
                </p>
              )}
            </div>

            <div className="md:col-span-2">
              <label className="mb-1.5 block text-xs text-slate-400">{f.interest}</label>
              <select className={inputCls} value={interest} onChange={(e) => setInterest(e.target.value)}>
                {f.interests.map((opt) => (
                  <option key={opt} value={opt} className="bg-ink-950 text-white">
                    {opt}
                  </option>
                ))}
              </select>
            </div>

            <div className="md:col-span-2">
              <label className="mb-1.5 block text-xs text-slate-400">{f.message}</label>
              <textarea
                className={`${inputCls} min-h-[88px] resize-y`}
                value={message}
                onChange={(e) => setMessage(e.target.value)}
                placeholder={f.messagePh}
                maxLength={1000}
              />
            </div>

            <div className="md:col-span-2 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
              <button
                type="submit"
                disabled={status === "sending" || !contactOk}
                className="inline-flex items-center justify-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-6 py-3 text-sm font-semibold text-ink-950 transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {status === "sending" ? (
                  <>
                    <Loader2 className="h-4 w-4 animate-spin" />
                    {f.submitting}
                  </>
                ) : (
                  <>
                    <Send className="h-4 w-4" />
                    {f.submit}
                  </>
                )}
              </button>
              <span className="text-[11px] text-slate-500">{f.privacy}</span>
            </div>

            {status === "error" && (
              <p className="md:col-span-2 text-sm text-rose-400">{f.error}</p>
            )}
          </motion.form>
        )}
      </AnimatePresence>
    </div>
  );
}
