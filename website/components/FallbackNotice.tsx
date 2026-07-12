"use client";

import { useEffect, useState } from "react";
import { LifeBuoy, Send, Users, Headphones } from "lucide-react";
import { useLang } from "./LanguageContext";
import { STABLE_TOUCHPOINTS } from "@/lib/domains";
import { getLocal, setLocal } from "@/lib/safe-storage";
import { track } from "@/lib/track";

// 域名防封 · 防走丢提示
// 私域站没有公开推广/搜索入口，域名一旦被封用户就找不回来。此组件把"永不被封的
// Telegram 触点"固定展示在页脚，并记住用户上次能打开的域名——引导用户「域名挂了回 TG 拿新址」。
const COPY = {
  zh: {
    title: "防走丢 · 私域访问",
    desc: "本站仅私域分发、不做公开推广。若某天此域名打不开（被封/DNS 拦截），请通过下方 Telegram 获取最新访问地址——Telegram 是我们永不失联的入口。",
    channel: "官方频道（最新地址）",
    group: "交流群",
    contact: "联系客服",
    lastSeen: "你上次访问的地址",
  },
  en: {
    title: "Never lose us · Private access",
    desc: "This site is private-only and not publicly promoted. If this domain ever stops loading (blocked/DNS-filtered), get the latest address via Telegram below — Telegram is our permanent fallback entry.",
    channel: "Official channel (latest URL)",
    group: "Community group",
    contact: "Contact support",
    lastSeen: "Your last working address",
  },
} as const;

export default function FallbackNotice() {
  const { lang } = useLang();
  const c = COPY[lang];
  const [lastHost, setLastHost] = useState("");

  useEffect(() => {
    try {
      const host = window.location.host;
      if (host) {
        setLocal("bl-last-host", host);
        setLastHost((getLocal("bl-last-host") as string) || host);
      }
    } catch {
      /* ignore */
    }
  }, []);

  const links = [
    { href: STABLE_TOUCHPOINTS.channel, label: c.channel, icon: Send, where: "fallback_channel" },
    { href: STABLE_TOUCHPOINTS.group, label: c.group, icon: Users, where: "fallback_group" },
    { href: STABLE_TOUCHPOINTS.contact, label: c.contact, icon: Headphones, where: "fallback_contact" },
  ];

  return (
    <div className="rounded-2xl border border-neon-violet/20 bg-neon-violet/[0.04] p-5">
      <div className="flex items-center gap-2 text-sm font-semibold text-neon-violet">
        <LifeBuoy className="h-4 w-4" />
        {c.title}
      </div>
      <p className="mt-2 text-xs leading-relaxed text-slate-400">{c.desc}</p>
      <div className="mt-4 flex flex-wrap gap-2">
        {links.map((l) => {
          const Icon = l.icon;
          return (
            <a
              key={l.where}
              href={l.href}
              target="_blank"
              rel="noreferrer"
              onClick={() => track("cta_click", { where: l.where })}
              className="inline-flex items-center gap-1.5 rounded-full border border-white/10 bg-white/[0.03] px-3 py-1.5 text-xs text-slate-200 transition hover:border-neon-cyan/40 hover:text-white"
            >
              <Icon className="h-3.5 w-3.5" />
              {l.label}
            </a>
          );
        })}
      </div>
      {lastHost && (
        <p className="mt-3 font-mono text-[11px] text-slate-600">
          {c.lastSeen}: <span className="text-slate-400">{lastHost}</span>
        </p>
      )}
    </div>
  );
}
