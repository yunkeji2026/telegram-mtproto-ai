"use client";

import { useEffect, useState } from "react";
import { Send, Tag } from "lucide-react";
import { useLang } from "./LanguageContext";
import { useTelegram } from "./TelegramProvider";
import { CONTACT_URL } from "@/lib/site";
import { track } from "@/lib/track";

export default function StickyCTA() {
  const { t } = useLang();
  const { isMiniApp } = useTelegram();
  const [show, setShow] = useState(false);

  if (isMiniApp) return null;

  useEffect(() => {
    const onScroll = () => setShow(window.scrollY > 600);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  return (
    <div
      className={`fixed inset-x-0 bottom-0 z-40 transition-transform duration-300 lg:hidden ${
        show ? "translate-y-0" : "translate-y-full"
      }`}
    >
      <div className="glass flex items-center gap-2 border-t border-white/10 px-3 py-2.5 pb-[calc(0.625rem+env(safe-area-inset-bottom))]">
        <a
          href="#pricing"
          className="flex flex-1 items-center justify-center gap-1.5 rounded-full border border-white/15 py-2.5 text-sm font-medium text-slate-200"
        >
          <Tag className="h-4 w-4" />
          {t.nav.pricing}
        </a>
        <a
          href={CONTACT_URL}
          target="_blank"
          rel="noreferrer"
          onClick={() => track("cta_click", { where: "sticky_mobile" })}
          className="flex flex-[1.4] items-center justify-center gap-1.5 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet py-2.5 text-sm font-semibold text-ink-950"
        >
          <Send className="h-4 w-4" />
          {t.nav.cta}
        </a>
      </div>
    </div>
  );
}
