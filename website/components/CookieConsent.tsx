"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useLang } from "./LanguageContext";
import { getLocal, setLocal } from "@/lib/safe-storage";

const KEY = "yt-cookie-consent";

export default function CookieConsent() {
  const { lang } = useLang();
  const zh = lang === "zh";
  const [show, setShow] = useState(false);

  useEffect(() => {
    // Defer to next tick so it never blocks first paint / LCP.
    const id = setTimeout(() => {
      if (!getLocal(KEY)) setShow(true);
    }, 800);
    return () => clearTimeout(id);
  }, []);

  function decide(value: "accept" | "reject") {
    setLocal(KEY, value);
    setShow(false);
  }

  if (!show) return null;

  return (
    <div
      role="dialog"
      aria-live="polite"
      aria-label={zh ? "Cookie 同意" : "Cookie consent"}
      className="fixed inset-x-3 bottom-3 z-[60] mx-auto max-w-2xl rounded-2xl border border-white/10 bg-ink-900/90 p-4 shadow-2xl backdrop-blur-md sm:inset-x-auto sm:left-1/2 sm:-translate-x-1/2"
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <p className="text-xs leading-relaxed text-slate-300">
          {zh
            ? "我们使用本地存储记住语言偏好，并用匿名统计改进网站。"
            : "We use local storage to remember your language and anonymous analytics to improve the site."}{" "}
          <Link href={zh ? "/privacy" : "/en/privacy"} className="text-cyan-300 underline-offset-2 hover:underline">
            {zh ? "隐私政策" : "Privacy Policy"}
          </Link>
        </p>
        <div className="flex shrink-0 items-center gap-2">
          <button
            onClick={() => decide("reject")}
            className="rounded-lg border border-slate-700 px-3 py-1.5 text-xs font-medium text-slate-300 transition hover:border-slate-600"
          >
            {zh ? "仅必要" : "Essential only"}
          </button>
          <button
            onClick={() => decide("accept")}
            className="rounded-lg bg-cyan-400 px-4 py-1.5 text-xs font-semibold text-ink-950 transition hover:bg-cyan-300"
          >
            {zh ? "接受" : "Accept"}
          </button>
        </div>
      </div>
    </div>
  );
}
