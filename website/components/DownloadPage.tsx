"use client";

import Link from "next/link";
import {
  ArrowRight,
  ArrowDown,
  Check,
  Send,
  Languages,
  ShieldCheck,
  Home,
  Monitor,
  KeyRound,
  MessagesSquare,
  Download,
} from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import BrandMark from "./BrandMark";
import Footer from "./Footer";
import { BRAND } from "@/lib/brand";
import { CONTACT_URL, DOWNLOAD_WIN_URL, DOWNLOAD_IS_INTERNAL, DESKTOP_VERSION, DESKTOP_SIZE_MB } from "@/lib/site";
import { track } from "@/lib/track";

/** 桌面客户端下载 + 免费试用引导页（/download、/en/download）。
 *  诚实原则：安装包 URL 未托管（DOWNLOAD_WIN_URL 空）时按钮引导联系客服而非死链；
 *  试用需人工发放 license key（无自助发卡），页面如实说明「联系获取 → 设置粘贴激活」。 */

type L = { zh: string; en: string };
const tx = (v: L, lang: "zh" | "en") => v[lang];

const STEPS: { icon: typeof Monitor; title: L; desc: L }[] = [
  {
    icon: Monitor,
    title: { zh: "① 安装", en: "① Install" },
    desc: {
      zh: "下载安装包，双击安装。内置本地服务，无需自己装 Python 或配置环境。",
      en: "Download and double-click to install. The local service is bundled — no Python or environment setup needed.",
    },
  },
  {
    icon: KeyRound,
    title: { zh: "② 填 AI Key", en: "② Add AI Key" },
    desc: {
      zh: "首启向导里填一个大模型 API Key（DeepSeek / OpenAI 兼容端点均可），测试通过即「翻译就绪」。",
      en: "The first-run wizard takes one LLM API key (DeepSeek / any OpenAI-compatible endpoint). Test passes → translation is ready.",
    },
  },
  {
    icon: MessagesSquare,
    title: { zh: "③ 接号开聊", en: "③ Connect & chat" },
    desc: {
      zh: "接入一个聊天账号，进入统一收件箱。收到外语消息自动翻译，AI 按你的人设拟稿或全自动回复。",
      en: "Connect an account and open the unified inbox. Foreign messages auto-translate; AI drafts or auto-replies in your persona.",
    },
  },
];

const REQUIREMENTS: L[] = [
  { zh: "Windows 10 / 11（64 位）", en: "Windows 10 / 11 (64-bit)" },
  { zh: "4GB 以上内存，1GB 可用磁盘", en: "4GB+ RAM, 1GB free disk" },
  { zh: "一个大模型 API Key（云或本地 OpenAI 兼容端点）", en: "One LLM API key (cloud or local OpenAI-compatible endpoint)" },
  { zh: "联网（调用翻译 / AI；数据仍落本地库）", en: "Internet (for translation / AI; data still stored locally)" },
];

const FAQ: { q: L; a: L }[] = [
  {
    q: { zh: "需要自己装 Python 吗？", en: "Do I need to install Python myself?" },
    a: {
      zh: "不需要。安装包已内置自包含的本地服务，双击安装即用，首次启动会自动拉起后台。",
      en: "No. The installer bundles a self-contained local service — install, and the backend starts itself on first launch.",
    },
  },
  {
    q: { zh: "免费试用怎么开通？", en: "How does the free trial work?" },
    a: {
      zh: "试用采用「字符额度」制。联系客服获取一枚试用授权码，在桌面端「设置」里粘贴激活即可，额度内的翻译 / 语音免费用；用尽后可续。目前授权码由客服发放（暂无自助签发）。",
      en: "The trial is metered by character quota. Contact us for a trial license key, paste it in Settings on the desktop app, and translation / voice within the quota are free; top up after. Keys are issued by our team for now (no self-serve yet).",
    },
  },
  {
    q: { zh: "数据会上传到你们服务器吗？", en: "Is my data uploaded to your servers?" },
    a: {
      zh: "客户档案与会话落在你自己机器的本地 SQLite 库；只有调用翻译 / AI 时把待处理文本发往你配置的模型端点。本地部署时数据不经我们的服务器。",
      en: "Customer files and conversations live in a local SQLite database on your machine; only the text being translated / answered goes to the model endpoint you configure. With on-prem deployment the data never touches our servers.",
    },
  },
  {
    q: { zh: "安装时提示未知发行者怎么办？", en: "Windows warns about an unknown publisher — is that OK?" },
    a: {
      zh: "当前版本尚未做代码签名，Windows SmartScreen 可能提示。可选「更多信息 → 仍要运行」。代码签名版在路线图上。",
      en: "This build is not yet code-signed, so SmartScreen may warn. Choose “More info → Run anyway”. A signed build is on the roadmap.",
    },
  },
  {
    q: { zh: "有 macOS 版吗？", en: "Is there a macOS version?" },
    a: {
      zh: "macOS 版在路线图上，暂未开放下载。需要请联系客服，我们会在就绪后通知你。",
      en: "A macOS build is on the roadmap but not yet available. Contact us and we'll notify you when it's ready.",
    },
  },
];

function DownloadNav() {
  const { lang, toggle } = useLang();
  const home = lang === "zh" ? "/" : "/en";
  return (
    <header className="fixed inset-x-0 top-0 z-50 glass">
      <nav className="mx-auto flex max-w-6xl items-center justify-between px-5 py-3.5">
        <div className="flex items-center gap-3">
          <Link href={home} className="flex items-center gap-2">
            <BrandMark className="h-8 w-8" />
            <span className="hidden text-base font-semibold tracking-wide text-white sm:inline">
              {BRAND.company.zh} <span className="text-slate-400">{BRAND.company.en}</span>
            </span>
          </Link>
          <span className="hidden rounded-full border border-white/10 bg-white/5 px-2.5 py-0.5 text-[11px] text-slate-300 md:inline">
            {lang === "zh" ? "智聊 ChatX 桌面端" : "ChatX Desktop"}
          </span>
        </div>
        <div className="flex items-center gap-2.5">
          <Link
            href={home}
            className="hidden items-center gap-1.5 rounded-full border border-white/15 px-3.5 py-1.5 text-xs text-slate-300 transition hover:text-white sm:inline-flex"
          >
            <Home className="h-3.5 w-3.5" />
            {lang === "zh" ? "返回首页" : "Home"}
          </Link>
          <button
            onClick={toggle}
            className="inline-flex items-center gap-1.5 rounded-full border border-white/15 px-3.5 py-1.5 text-xs text-slate-300 transition hover:text-white"
            aria-label="switch language"
          >
            <Languages className="h-3.5 w-3.5" />
            {lang === "zh" ? "EN" : "中文"}
          </button>
          <a
            href={CONTACT_URL}
            target="_blank"
            rel="noreferrer"
            onClick={() => track("cta_click", { where: "download_nav" })}
            className="inline-flex items-center gap-1.5 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-4 py-1.5 text-xs font-semibold text-ink-950 transition hover:opacity-90"
          >
            <Send className="h-3.5 w-3.5" />
            {lang === "zh" ? "在线咨询" : "Chat now"}
          </a>
        </div>
      </nav>
    </header>
  );
}

function DownloadButton({ where }: { where: string }) {
  const { lang } = useLang();
  const label = lang === "zh" ? "下载 Windows 版" : "Download for Windows";
  return (
    <a
      href={DOWNLOAD_WIN_URL}
      // 站内相对路径：加 download 属性直接落盘、同标签页；外部 CDN 绝对 URL：新开标签页。
      {...(DOWNLOAD_IS_INTERNAL
        ? { download: `ChatX-Setup-${DESKTOP_VERSION}.exe` }
        : { target: "_blank", rel: "noreferrer" })}
      onClick={() => track("download_click", { where, internal: DOWNLOAD_IS_INTERNAL })}
      className="group inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-7 py-3 font-semibold text-ink-950 transition hover:opacity-90"
    >
      <Download className="h-4 w-4" />
      {label}
      <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-1" />
    </a>
  );
}

export default function DownloadPage() {
  const { lang } = useLang();

  return (
    <main className="relative min-h-screen">
      <DownloadNav />

      {/* Hero */}
      <section className="relative overflow-hidden px-5 pb-16 pt-28 md:pt-32">
        <div className="mx-auto max-w-3xl text-center">
          <Reveal>
            <span className="inline-flex items-center gap-1.5 rounded-full border border-neon-cyan/30 bg-neon-cyan/10 px-3.5 py-1 text-xs font-medium text-neon-cyan">
              <Monitor className="h-3.5 w-3.5" />
              {lang === "zh" ? "智聊 ChatX · Windows 桌面端" : "ChatX · Windows Desktop"}
            </span>
          </Reveal>
          <Reveal delay={0.05}>
            <h1 className="mt-5 text-4xl font-bold leading-tight text-white md:text-5xl">
              <span className="whitespace-nowrap">{lang === "zh" ? "装上就能用，" : "Install and go —"}</span>{" "}
              <span className="text-gradient whitespace-nowrap">
                {lang === "zh" ? "10 分钟开聊" : "chatting in 10 minutes"}
              </span>
            </h1>
          </Reveal>
          <Reveal delay={0.1}>
            <p className="mx-auto mt-5 max-w-2xl text-base text-slate-400 md:text-lg">
              {lang === "zh"
                ? "多语种 AI 员工桌面端：内置本地服务免装环境，填一个 AI Key 即翻译生效，接号进统一收件箱，AI 以你的人设 7×24 接客。"
                : "The multilingual AI-employee desktop app: bundled local service (no setup), add one AI key to enable translation, connect an account into the unified inbox, and let AI serve 24/7 in your persona."}
            </p>
          </Reveal>
          <Reveal delay={0.2}>
            <div className="mt-8 flex flex-col items-center justify-center gap-3 sm:flex-row">
              <DownloadButton where="download_hero" />
              <a
                href="#how"
                className="inline-flex items-center gap-2 rounded-full border border-white/15 px-7 py-3 font-medium text-slate-200 transition hover:border-neon-cyan/50 hover:text-white"
              >
                {lang === "zh" ? "怎么开始" : "How it works"}
                <ArrowDown className="h-4 w-4" />
              </a>
            </div>
          </Reveal>
          <Reveal delay={0.25}>
            <p className="mt-5 flex flex-wrap items-center justify-center gap-x-4 gap-y-1 text-xs text-slate-500">
              <span className="inline-flex items-center gap-1.5">
                <Monitor className="h-3.5 w-3.5 text-slate-400" />
                {lang === "zh" ? "Windows 10 / 11（64 位）" : "Windows 10 / 11 (64-bit)"}
              </span>
              <span className="text-slate-700">·</span>
              <span>{lang === "zh" ? `版本 v${DESKTOP_VERSION}` : `Version v${DESKTOP_VERSION}`}</span>
              <span className="text-slate-700">·</span>
              <span>{lang === "zh" ? `约 ${DESKTOP_SIZE_MB}MB` : `~${DESKTOP_SIZE_MB}MB`}</span>
            </p>
          </Reveal>
          <Reveal delay={0.3}>
            <p className="mt-4 flex items-center justify-center gap-2 text-xs text-slate-500">
              <ShieldCheck className="h-3.5 w-3.5 text-emerald-400/80" />
              {lang === "zh"
                ? "本地部署 · 客户数据落你自己机器 · USDT 结算"
                : "Private deployment · your customer data stays on your machine · USDT settlement"}
            </p>
          </Reveal>
        </div>
      </section>

      {/* How — 三步上手 */}
      <section id="how" className="scroll-mt-24 border-y border-white/5 bg-white/[0.015] px-5 py-16">
        <div className="mx-auto max-w-4xl">
          <Reveal className="text-center">
            <h2 className="text-2xl font-bold text-white md:text-3xl">
              {lang === "zh" ? "三步上手" : "Three steps to start"}
            </h2>
            <p className="mx-auto mt-2 max-w-xl text-sm text-slate-400">
              {lang === "zh"
                ? "从安装到第一条自动翻译，全程不碰命令行。"
                : "From install to your first auto-translated message — no command line."}
            </p>
          </Reveal>
          <div className="mt-9 grid gap-4 md:grid-cols-3">
            {STEPS.map((s, i) => {
              const Icon = s.icon;
              return (
                <Reveal key={s.title.en} delay={i * 0.06}>
                  <div className="relative h-full rounded-2xl border border-white/10 bg-ink-900/50 p-5 pt-6">
                    <span className="absolute -top-3.5 left-5 grid h-7 w-7 place-items-center rounded-full bg-gradient-to-br from-neon-cyan to-neon-violet text-ink-950">
                      <Icon className="h-4 w-4" />
                    </span>
                    <h3 className="font-semibold text-white">{tx(s.title, lang)}</h3>
                    <p className="mt-2 text-sm leading-relaxed text-slate-400">{tx(s.desc, lang)}</p>
                  </div>
                </Reveal>
              );
            })}
          </div>
        </div>
      </section>

      {/* Trial + Requirements */}
      <section className="px-5 py-16">
        <div className="mx-auto grid max-w-5xl gap-5 md:grid-cols-2">
          <Reveal>
            <div className="flex h-full flex-col rounded-2xl border border-neon-cyan/25 bg-gradient-to-br from-neon-cyan/[0.06] to-neon-violet/[0.06] p-6">
              <h3 className="flex items-center gap-2 text-lg font-semibold text-white">
                <KeyRound className="h-5 w-5 text-neon-cyan" />
                {lang === "zh" ? "免费试用（额度制）" : "Free trial (metered)"}
              </h3>
              <ol className="mt-4 flex-1 space-y-3 text-sm text-slate-300">
                <li className="flex gap-2.5">
                  <span className="mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-full bg-white/10 text-[11px] font-bold text-neon-cyan">1</span>
                  {lang === "zh" ? "联系客服领取一枚试用授权码（含免费字符额度）。" : "Contact us for a trial license key (includes a free character quota)."}
                </li>
                <li className="flex gap-2.5">
                  <span className="mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-full bg-white/10 text-[11px] font-bold text-neon-cyan">2</span>
                  {lang === "zh" ? "桌面端「设置」里粘贴授权码激活，状态变为已授权。" : "Paste the key in Settings on the desktop app to activate."}
                </li>
                <li className="flex gap-2.5">
                  <span className="mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-full bg-white/10 text-[11px] font-bold text-neon-cyan">3</span>
                  {lang === "zh" ? "额度内的翻译 / 语音免费用，用尽后按套餐续。" : "Translation / voice within the quota are free; top up by plan after."}
                </li>
              </ol>
              <div className="mt-5 flex flex-col gap-3 sm:flex-row">
                <a
                  href={CONTACT_URL}
                  target="_blank"
                  rel="noreferrer"
                  onClick={() => track("cta_click", { where: "download_trial" })}
                  className="inline-flex items-center justify-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-5 py-2.5 text-sm font-semibold text-ink-950 transition hover:opacity-90"
                >
                  <Send className="h-4 w-4" />
                  {lang === "zh" ? "领取试用授权码" : "Get a trial key"}
                </a>
                <Link
                  href={lang === "zh" ? "/#pricing" : "/en#pricing"}
                  className="inline-flex items-center justify-center gap-2 rounded-full border border-white/15 px-5 py-2.5 text-sm font-medium text-slate-200 transition hover:border-neon-cyan/50 hover:text-white"
                >
                  {lang === "zh" ? "查看套餐" : "See plans"}
                </Link>
              </div>
            </div>
          </Reveal>

          <Reveal delay={0.08}>
            <div className="flex h-full flex-col rounded-2xl border border-white/10 bg-ink-900/50 p-6">
              <h3 className="flex items-center gap-2 text-lg font-semibold text-white">
                <Monitor className="h-5 w-5 text-slate-300" />
                {lang === "zh" ? "系统要求" : "Requirements"}
              </h3>
              <ul className="mt-4 flex-1 space-y-2.5 text-sm text-slate-300">
                {REQUIREMENTS.map((r) => (
                  <li key={r.en} className="flex items-start gap-2.5">
                    <Check className="mt-0.5 h-4 w-4 shrink-0 text-emerald-400" />
                    {tx(r, lang)}
                  </li>
                ))}
              </ul>
              <p className="mt-5 flex items-center gap-2 text-[11px] text-slate-500">
                <ShieldCheck className="h-3.5 w-3.5 shrink-0 text-emerald-400/80" />
                {lang === "zh"
                  ? "本版暂未代码签名，SmartScreen 可能提示；macOS 版在路线图上。"
                  : "This build is not code-signed yet (SmartScreen may warn); macOS is on the roadmap."}
              </p>
            </div>
          </Reveal>
        </div>
      </section>

      {/* FAQ */}
      <section className="border-y border-white/5 bg-white/[0.015] px-5 py-16">
        <div className="mx-auto max-w-3xl">
          <Reveal className="text-center">
            <h2 className="text-2xl font-bold text-white md:text-3xl">{lang === "zh" ? "常见问题" : "FAQ"}</h2>
          </Reveal>
          <div className="mt-8 space-y-3">
            {FAQ.map((f, i) => (
              <Reveal key={f.q.en} delay={i * 0.04}>
                <details className="group rounded-2xl border border-white/10 bg-ink-900/50 p-5 open:border-neon-cyan/25">
                  <summary className="cursor-pointer list-none font-medium text-white marker:hidden">
                    {tx(f.q, lang)}
                  </summary>
                  <p className="mt-3 text-sm leading-relaxed text-slate-400">{tx(f.a, lang)}</p>
                </details>
              </Reveal>
            ))}
          </div>
        </div>
      </section>

      {/* Final CTA */}
      <section className="px-5 py-20">
        <Reveal className="mx-auto max-w-3xl">
          <div className="relative overflow-hidden rounded-3xl border border-neon-cyan/30 bg-gradient-to-br from-neon-cyan/[0.08] to-neon-violet/[0.08] p-8 text-center md:p-12">
            <h2 className="text-2xl font-bold text-white md:text-3xl">
              {lang === "zh" ? "现在就装上试试" : "Install it now"}
            </h2>
            <p className="mx-auto mt-3 max-w-xl text-sm leading-relaxed text-slate-300">
              {lang === "zh"
                ? "装上、填 Key、接一个号——10 分钟看到第一条自动翻译。装不上或要试用授权码，随时找客服。"
                : "Install, add a key, connect one account — see your first auto-translation in 10 minutes. Stuck, or need a trial key? Reach out any time."}
            </p>
            <div className="mt-7 flex flex-col items-center justify-center gap-3 sm:flex-row">
              <DownloadButton where="download_final" />
              <a
                href={CONTACT_URL}
                target="_blank"
                rel="noreferrer"
                onClick={() => track("cta_click", { where: "download_final_contact" })}
                className="inline-flex items-center gap-2 rounded-full border border-white/15 px-7 py-3 font-medium text-slate-200 transition hover:border-neon-cyan/50 hover:text-white"
              >
                <Send className="h-4 w-4" />
                {lang === "zh" ? "Telegram 咨询" : "Ask on Telegram"}
              </a>
            </div>
          </div>
        </Reveal>
      </section>

      <Footer />
    </main>
  );
}
