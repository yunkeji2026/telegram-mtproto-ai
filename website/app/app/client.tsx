"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { content } from "@/lib/content";
import { CONTACT_URL } from "@/lib/site";
import { track } from "@/lib/track";
import { resolveView, TABS, type View } from "./routing";
import { AiChat, EngageView, HomeView, LeadForm, LiveAvatarView, PricingView, SoulSyncView } from "./views";

type Lang = "zh" | "en";

function haptic(kind: "light" | "success" = "light") {
  try {
    const h = window.Telegram?.WebApp?.HapticFeedback;
    if (kind === "success") h?.notificationOccurred?.("success");
    else h?.impactOccurred?.("light");
  } catch {
    /* non-fatal */
  }
}

function openLink(url: string) {
  try {
    const tg = window.Telegram?.WebApp as { openTelegramLink?: (u: string) => void } | undefined;
    if (url.startsWith("https://t.me/") && tg?.openTelegramLink) {
      tg.openTelegramLink(url);
      return;
    }
  } catch {
    /* fall through */
  }
  window.open(url, "_blank");
}

export default function MiniAppClient({ initialView }: { initialView: View }) {
  const [lang, setLang] = useState<Lang>("zh");
  const [view, setViewState] = useState<View>(initialView);
  const [initData, setInitData] = useState("");
  const [leadName, setLeadName] = useState("");
  const [leadContact, setLeadContact] = useState("");
  const [leadInterest, setLeadInterest] = useState("");
  const [gate, setGate] = useState<{ channel: boolean; group: boolean; code: string; checked: boolean }>({
    channel: false,
    group: false,
    code: "",
    checked: false,
  });
  const [verifying, setVerifying] = useState(false);
  const opened = useRef(false);

  const setView = useCallback((v: View, withHaptic = true) => {
    if (withHaptic) haptic("light");
    setViewState((prev) => {
      if (prev !== v) track("miniapp_view", { view: v });
      return v;
    });
    try {
      const url = new URL(window.location.href);
      if (v === "home") url.searchParams.delete("view");
      else url.searchParams.set("view", v);
      window.history.replaceState(null, "", url.toString());
    } catch {
      /* ignore */
    }
    window.scrollTo({ top: 0, behavior: "smooth" });
  }, []);

  // init Telegram + analytics + startapp deep-link fallback
  useEffect(() => {
    let startParam: string | null = null;
    let platform = "web";
    try {
      const tg = window.Telegram?.WebApp;
      if (tg) {
        tg.ready();
        tg.expand();
        platform = tg.platform || "tg";
        if (tg.initData) setInitData(tg.initData);
        const u = tg.initDataUnsafe?.user;
        if (u?.first_name) setLeadName(u.first_name);
        if (u?.username) setLeadContact(`@${u.username}`);
        if (u?.language_code && !u.language_code.startsWith("zh")) setLang("en");
        startParam = tg.initDataUnsafe?.start_param ?? null;
      } else if (typeof navigator !== "undefined" && navigator.language.startsWith("en")) {
        setLang("en");
      }
    } catch {
      /* non-fatal */
    }

    // startapp 深链：仅当 URL 未显式带 ?view 时，用 start_param 决定视图（不覆盖显式入口）
    let landed = initialView;
    try {
      const hasViewParam = new URLSearchParams(window.location.search).has("view");
      if (!hasViewParam && startParam) {
        const v = resolveView(startParam);
        if (v !== initialView) {
          landed = v;
          setView(v, false);
        }
      }
    } catch {
      /* ignore */
    }

    if (!opened.current) {
      opened.current = true;
      track("miniapp_open", { view: landed, source: startParam || "direct", platform });
    }
  }, [initialView, setView]);

  // Telegram BackButton: show off-home, returns to overview
  useEffect(() => {
    const bb = window.Telegram?.WebApp?.BackButton;
    if (!bb) return;
    const onBack = () => setView("home");
    try {
      if (view === "home") {
        bb.hide();
      } else {
        bb.show();
        bb.onClick(onBack);
      }
    } catch {
      /* ignore */
    }
    return () => {
      try {
        bb.offClick(onBack);
      } catch {
        /* ignore */
      }
    };
  }, [view, setView]);

  const zh = lang === "zh";

  const goContact = useCallback(
    (interest: string) => {
      setLeadInterest(interest);
      track("miniapp_cta", { view, interest });
      openLink(CONTACT_URL);
    },
    [view],
  );

  // Telegram MainButton: per-view sticky primary CTA
  useEffect(() => {
    const tg = window.Telegram?.WebApp;
    const mb = tg?.MainButton as
      | (NonNullable<typeof tg>["MainButton"] & {
          setParams?: (p: { text?: string; color?: string; text_color?: string; is_visible?: boolean }) => void;
        })
      | undefined;
    if (!mb) return;

    const cfg: Record<View, { text: string; action: () => void }> = {
      home: { text: zh ? "🤖 问 AI 客服" : "🤖 Ask the AI", action: () => document.getElementById("ai-chat")?.scrollIntoView({ behavior: "smooth" }) },
      liveavatar: { text: zh ? "🎬 预约换脸演示" : "🎬 Book a demo", action: () => goContact(zh ? "华影 · 实时换脸咨询" : "LiveAvatar demo") },
      soulsync: { text: zh ? "💬 免费试用 AI 成交" : "💬 Try AI closing", action: () => goContact(zh ? "灵犀 · AI 成交试用" : "SoulSync trial") },
      pricing: { text: zh ? "🎁 领专属折扣码" : "🎁 Claim discount code", action: () => document.getElementById("unlock")?.scrollIntoView({ behavior: "smooth" }) },
      engage: { text: zh ? "🤝 联系定制顾问" : "🤝 Talk to advisor", action: () => goContact(zh ? "合作方式咨询" : "Engagement inquiry") },
    };
    const { text, action } = cfg[view];
    const onClick = () => {
      haptic("light");
      track("miniapp_cta", { view, kind: "mainbutton" });
      action();
    };

    try {
      if (typeof mb.setParams === "function") {
        mb.setParams({ text, color: "#22d3ee", text_color: "#05060f", is_visible: true });
      } else {
        mb.text = text;
        mb.color = "#22d3ee";
        mb.textColor = "#05060f";
        mb.show();
      }
      mb.onClick(onClick);
    } catch {
      /* MainButton unavailable — inline CTAs still work */
    }
    return () => {
      try {
        mb.offClick(onClick);
      } catch {
        /* ignore */
      }
    };
  }, [view, zh, goContact]);

  const verifyUnlock = useCallback(async () => {
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
        const unlocked = Boolean(data.channel) && Boolean(data.group);
        setGate({ channel: Boolean(data.channel), group: Boolean(data.group), code: data.code ? String(data.code) : "", checked: true });
        if (unlocked) {
          haptic("success");
          track("miniapp_unlock", { code: Boolean(data.code) });
        }
      }
    } catch {
      /* ignore */
    } finally {
      setVerifying(false);
    }
  }, [initData, verifying]);

  const t = content[lang];

  return (
    <main className="mx-auto min-h-screen max-w-lg bg-slate-950 px-4 pb-28 pt-4 text-slate-100">
      <header className="flex items-center justify-between">
        <div>
          <div className="text-lg font-bold">华灵科技 HuaLing Tech</div>
          <div className="text-[11px] text-cyan-300">{zh ? "华影 LiveAvatar × 灵犀 SoulSync · USDT 结算" : "HuaYing LiveAvatar × LingXi SoulSync · USDT"}</div>
        </div>
        <button onClick={() => setLang(zh ? "en" : "zh")} className="rounded-lg border border-slate-700 px-2 py-1 text-xs text-slate-300">
          {zh ? "EN" : "中文"}
        </button>
      </header>

      <div className="mt-4">
        {view === "home" && (
          <>
            <HomeView t={t} zh={zh} onGo={setView} />
            <div className="mt-4">
              <AiChat t={t} zh={zh} />
            </div>
            <div className="mt-4">
              <LeadForm t={t} zh={zh} presetInterest={leadInterest} view={view} name={leadName} setName={setLeadName} contact={leadContact} setContact={setLeadContact} />
            </div>
          </>
        )}
        {view === "liveavatar" && <LiveAvatarView t={t} zh={zh} onContact={goContact} />}
        {view === "soulsync" && <SoulSyncView t={t} zh={zh} onContact={goContact} />}
        {view === "pricing" && <PricingView t={t} zh={zh} gate={gate} initData={initData} verifying={verifying} onVerify={verifyUnlock} />}
        {view === "engage" && <EngageView t={t} zh={zh} onContact={goContact} />}
      </div>

      {/* bottom tab bar */}
      <nav className="fixed inset-x-0 bottom-0 z-20 mx-auto max-w-lg border-t border-slate-800 bg-slate-950/95 backdrop-blur">
        <div className="grid grid-cols-5">
          {TABS.map((tab) => {
            const active = view === tab.id;
            return (
              <button
                key={tab.id}
                onClick={() => setView(tab.id)}
                className={`flex flex-col items-center gap-0.5 py-2 text-[10px] transition ${active ? "text-cyan-300" : "text-slate-500"}`}
              >
                <span className={`text-lg leading-none transition ${active ? "scale-110" : ""}`}>{tab.icon}</span>
                <span className={active ? "font-medium" : ""}>{zh ? tab.zh : tab.en}</span>
              </button>
            );
          })}
        </div>
      </nav>
    </main>
  );
}
