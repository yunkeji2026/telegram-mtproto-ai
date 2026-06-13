"use client";

import { createContext, useContext, useEffect, useState, ReactNode, useCallback } from "react";

export interface TgUser {
  id: number;
  first_name?: string;
  last_name?: string;
  username?: string;
  language_code?: string;
}

interface TelegramContextValue {
  isMiniApp: boolean;
  ready: boolean;
  user: TgUser | null;
  startParam: string | null;
  initData: string;
  scrollToSection: (id: string) => void;
}

const TelegramContext = createContext<TelegramContextValue>({
  isMiniApp: false,
  ready: false,
  user: null,
  startParam: null,
  initData: "",
  scrollToSection: () => {},
});

declare global {
  interface Window {
    Telegram?: {
      WebApp: {
        ready: () => void;
        expand: () => void;
        close: () => void;
        MainButton: {
          text: string;
          color: string;
          textColor: string;
          isVisible: boolean;
          show: () => void;
          hide: () => void;
          onClick: (cb: () => void) => void;
          offClick: (cb: () => void) => void;
        };
        BackButton: {
          isVisible: boolean;
          show: () => void;
          hide: () => void;
          onClick: (cb: () => void) => void;
          offClick: (cb: () => void) => void;
        };
        initData?: string;
        initDataUnsafe?: {
          user?: TgUser;
          start_param?: string;
        };
        themeParams?: Record<string, string>;
        colorScheme?: "light" | "dark";
        platform?: string;
        HapticFeedback?: {
          impactOccurred?: (style: string) => void;
          notificationOccurred?: (type: string) => void;
          selectionChanged?: () => void;
        };
      };
    };
  }
}

const START_MAP: Record<string, string> = {
  pricing: "pricing",
  autochat: "autochat",
  realtime: "realtime",
  engage: "engage",
  contact: "contact",
  cases: "cases",
  showcase: "showcase",
};

export function TelegramProvider({ children }: { children: ReactNode }) {
  const [isMiniApp, setIsMiniApp] = useState(false);
  const [ready, setReady] = useState(false);
  const [user, setUser] = useState<TgUser | null>(null);
  const [startParam, setStartParam] = useState<string | null>(null);
  const [initData, setInitData] = useState("");

  const scrollToSection = useCallback((id: string) => {
    const el = document.getElementById(id);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
  }, []);

  useEffect(() => {
    const tg = window.Telegram?.WebApp;
    const inTg = Boolean(tg?.platform && tg.platform !== "unknown");
    if (!tg || !inTg) {
      setReady(true);
      return;
    }

    setIsMiniApp(true);
    tg.ready();
    tg.expand();

    if (tg.initData) setInitData(tg.initData);

    const u = tg.initDataUnsafe?.user;
    if (u?.id) setUser(u);

    const sp = tg.initDataUnsafe?.start_param ?? null;
    setStartParam(sp);

    // sync TG theme hints
    const tp = tg.themeParams;
    if (tp?.bg_color) {
      document.documentElement.style.setProperty("--tg-bg", tp.bg_color);
    }

    // deep link from bot ?startapp=pricing or start_param
    const target = sp ? START_MAP[sp] ?? sp : null;
    if (target) {
      setTimeout(() => scrollToSection(target), 600);
    }

    setReady(true);
  }, [scrollToSection]);

  return (
    <TelegramContext.Provider value={{ isMiniApp, ready, user, startParam, initData, scrollToSection }}>
      {children}
    </TelegramContext.Provider>
  );
}

export function useTelegram() {
  return useContext(TelegramContext);
}
