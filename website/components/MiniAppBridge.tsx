"use client";

import { useEffect, useRef } from "react";
import { useLang } from "./LanguageContext";
import { useTelegram } from "./TelegramProvider";

/** Wires TG MainButton → scroll to contact / lead form. */
export default function MiniAppBridge() {
  const { isMiniApp } = useTelegram();
  const { lang } = useLang();
  const handlerRef = useRef(() => {
    const el = document.getElementById("contact");
    el?.scrollIntoView({ behavior: "smooth", block: "start" });
    setTimeout(() => {
      const input = document.querySelector<HTMLInputElement>("#lead-contact");
      input?.focus();
    }, 400);
  });

  useEffect(() => {
    if (!isMiniApp) return;
    const tg = window.Telegram?.WebApp;
    if (!tg?.MainButton) return;

    const mb = tg.MainButton as typeof tg.MainButton & {
      setParams?: (p: { text?: string; color?: string; text_color?: string; is_visible?: boolean }) => void;
    };
    const label = lang === "zh" ? "提交留资 · 等客服联系" : "Submit lead · get contacted";
    const onClick = handlerRef.current;

    try {
      // prefer modern setParams; fall back to legacy property assignment
      if (typeof mb.setParams === "function") {
        mb.setParams({ text: label, color: "#22d3ee", text_color: "#05060f", is_visible: true });
      } else {
        mb.text = label;
        mb.color = "#22d3ee";
        mb.textColor = "#05060f";
        mb.show();
      }
      mb.onClick(onClick);
    } catch {
      /* MainButton API not available on this client — non-fatal */
    }

    return () => {
      try {
        mb.offClick(onClick);
        mb.hide();
      } catch {
        /* ignore */
      }
    };
  }, [isMiniApp, lang]);

  return null;
}
