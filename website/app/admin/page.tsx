"use client";

import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import {
  LayoutDashboard,
  TrendingUp,
  MessagesSquare,
  Megaphone,
  Ticket,
  Activity,
  RefreshCw,
  LogOut,
  Lock,
  ShieldCheck,
  CheckCircle2,
  XCircle,
  Search,
  Download,
  Copy,
  ExternalLink,
  ArrowUpRight,
  ArrowDownRight,
  ClipboardCheck,
  UserPlus,
  ListChecks,
} from "lucide-react";

export const dynamic = "force-dynamic";

interface Stats {
  totals: {
    pageviews: number;
    ctaClicks: number;
    leadSubmitEvents: number;
    leads: number;
    events: number;
    chats: number;
    convRate: number;
  };
  recentQuestions: { t: string; q: string; lang: string; source: string }[];
  last7d: { pageviews: number; ctaClicks: number; leads: number };
  ctaByWhere: Record<string, number>;
  leadsBySource: Record<string, number>;
  leadsByInterest: Record<string, number>;
  leadsByDay: Record<string, number>;
  statusCounts?: Record<string, number>;
  unlocks?: { issued: number; redeemed: number; pending?: number; expired?: number };
  recentLeads: {
    id: string;
    t: string;
    name: string;
    contact: string;
    interest: string;
    source: string;
    lang: string;
    status: LeadStatus;
    count: number;
  }[];
  chatCluster?: {
    total: number;
    coverage: number;
    topics: { id: string; label: string; count: number; samples: string[] }[];
    langs: { lang: string; count: number }[];
    uncovered: { q: string; count: number; lang: string }[];
  };
  series?: { days: string[]; pv: number[]; cta: number[]; leads: number[] };
  wow?: {
    pageviews: { cur: number; prev: number };
    ctaClicks: { cur: number; prev: number };
    leads: { cur: number; prev: number };
  };
  publishes?: {
    t: string;
    day: string;
    kind: "broadcast" | "catalog" | "daily" | "scheduled";
    target: string;
    summary: string;
    ref: { pvSame: number | null; leadSame: number | null; leadNext: number | null };
  }[];
  publishDays?: number[];
  miniapp?: {
    opens: number;
    cta: number;
    leads: number;
    unlocks: number;
    chats?: number;
    viewVisits: Record<string, number>;
    ctaByView: Record<string, number>;
    openBySource: Record<string, number>;
    funnel?: {
      sessions: number;
      engaged: number;
      intent: number;
      convert: number;
      rates: { engaged: number; intent: number; convert: number; overall: number };
    };
    byLanding?: { key: string; sessions: number; convert: number; rate: number }[];
    bySource?: { key: string; sessions: number; convert: number; rate: number }[];
    gateSteps?: Record<string, number>;
    leadFlow?: { start: number; done: number; abandonRate: number };
    dropped?: number;
    window?: number;
    series?: { open: number[]; cta: number[]; lead: number[] };
    wow?: {
      opens: { cur: number; prev: number };
      cta: { cur: number; prev: number };
      leads: { cur: number; prev: number };
    };
  };
}

// Mini App 视图 id → 中文标签（用于把埋点里的 view 键名展示成人话）。
const MINIAPP_VIEW_LABELS: Record<string, string> = {
  home: "概览",
  liveavatar: "视觉分身",
  soulsync: "智聊沟通",
  pricing: "价格",
  engage: "合作",
};
// 解锁三步埋点键名 → 人话（定位解锁流失在哪一步）。
const GATE_LABELS: Record<string, string> = {
  channel: "点关注频道",
  group: "点加入群",
  verify_ok: "校验通过",
  verify_fail: "校验未过",
};
function relabel(data: Record<string, number>, map: Record<string, string>): Record<string, number> {
  const out: Record<string, number> = {};
  for (const [k, v] of Object.entries(data)) out[map[k] ?? k] = v;
  return out;
}

const LANG_NAMES: Record<string, string> = {
  zh: "中文",
  en: "英语",
  es: "西班牙语",
  pt: "葡萄牙语",
  fr: "法语",
  de: "德语",
  ru: "俄语",
  ar: "阿拉伯语",
  th: "泰语",
  ja: "日语",
  ko: "韩语",
  he: "希伯来语",
  id: "印尼语",
  vi: "越南语",
};

function Stat({ label, value, sub }: { label: string; value: string | number; sub?: string }) {
  return (
    <div className="rounded-xl border border-slate-700 bg-slate-900 p-4">
      <div className="text-xs text-slate-400">{label}</div>
      <div className="mt-1 text-2xl font-bold text-white">{value}</div>
      {sub && <div className="mt-0.5 text-xs text-slate-500">{sub}</div>}
    </div>
  );
}

function HealthCell({ label, ok, text }: { label: string; ok?: boolean; text: string }) {
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-950/50 p-2">
      <div className="text-slate-500">{label}</div>
      <div className={`mt-0.5 font-medium ${ok ? "text-emerald-300" : "text-rose-300"}`}>
        {ok ? "●" : "○"} {text}
      </div>
    </div>
  );
}

function Bars({ title, data }: { title: string; data: Record<string, number> }) {
  const entries = Object.entries(data).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...entries.map((e) => e[1]));
  return (
    <div className="rounded-xl border border-slate-700 bg-slate-900 p-4">
      <div className="mb-3 text-sm font-semibold text-slate-200">{title}</div>
      {entries.length === 0 && <div className="text-xs text-slate-500">暂无数据</div>}
      <div className="space-y-2">
        {entries.map(([k, v]) => (
          <div key={k} className="flex items-center gap-2" title={`${k}：${v}`}>
            <span className="w-32 shrink-0 truncate text-xs text-slate-400" title={k}>
              {k}
            </span>
            <div className="h-4 flex-1 overflow-hidden rounded bg-slate-800">
              <div
                className="h-full rounded bg-cyan-500"
                style={{ width: `${(v / max) * 100}%` }}
              />
            </div>
            <span className="w-10 shrink-0 text-right text-xs tabular-nums text-slate-300">{v}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

interface KbEntry {
  id: string;
  q: string;
  a: string;
  lang?: string;
  t: string;
}

type BroadcastTarget = "channel" | "group" | "both";

interface ScheduledPost {
  id: string;
  text: string;
  target: BroadcastTarget;
  withButton: boolean;
  runAt: string;
  status: "pending" | "sent" | "failed";
  sentAt?: string;
  error?: string;
}

interface Template {
  id: string;
  name: string;
  text: string;
  target: BroadcastTarget;
  withButton: boolean;
}

interface DraftPost {
  id: string;
  text: string;
  theme?: string;
  source: string;
  createdAt: string;
}

interface HealthSnapshot {
  healthy: boolean;
  status: string;
  reasons: string[];
  checks: {
    storage?: { ok: boolean };
    deepseek?: { enabled: boolean; circuit: string; totalTrips: number };
    usage?: { count: number; cap: number; remaining: number };
    telegram?: { ok: boolean; bot?: string };
  };
}

interface AlertItem {
  t: string;
  kind: "degrade" | "recover" | "digest";
  reasons: string[];
  delivered: boolean;
  consec?: number;
  escalated?: boolean;
}

const TARGET_LABEL: Record<BroadcastTarget, string> = {
  channel: "频道",
  group: "群",
  both: "频道+群",
};

// ── content calendar (operator timezone, UTC+8) ──
const CAL_TZ_MS = 8 * 3600 * 1000;
const WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

/** Local (UTC+8) calendar-day key YYYY-MM-DD for an ISO timestamp. */
function localDayKey(iso: string): string {
  const t = Date.parse(iso);
  if (isNaN(t)) return "";
  return new Date(t + CAL_TZ_MS).toISOString().slice(0, 10);
}

/** Local (UTC+8) HH:MM for an ISO timestamp. */
function localHM(iso: string): string {
  const t = Date.parse(iso);
  if (isNaN(t)) return "";
  return new Date(t + CAL_TZ_MS).toISOString().slice(11, 16);
}

/** Today's local (UTC+8) day key. */
function todayLocalKey(): string {
  return new Date(Date.now() + CAL_TZ_MS).toISOString().slice(0, 10);
}

/** Monday-anchored start-of-week day key for a given local day key. */
function weekStartKey(dayKey: string): string {
  const base = Date.parse(dayKey + "T00:00:00Z");
  const dow = (new Date(base).getUTCDay() + 6) % 7; // 0=Mon
  return new Date(base - dow * 86400000).toISOString().slice(0, 10);
}

/** Add n days to a local day key. */
function addDaysKey(dayKey: string, n: number): string {
  return new Date(Date.parse(dayKey + "T00:00:00Z") + n * 86400000).toISOString().slice(0, 10);
}

/**
 * New ISO timestamp that keeps the original local time-of-day but moves to `dayKey`.
 * Used when an operator drags a scheduled post onto another calendar day.
 */
function moveToDayKeepTime(iso: string, dayKey: string): string {
  const localWall = Date.parse(iso) + CAL_TZ_MS; // local wall-clock ms (as if UTC)
  const tod = ((localWall % 86400000) + 86400000) % 86400000;
  const base = Date.parse(dayKey + "T00:00:00Z");
  return new Date(base + tod - CAL_TZ_MS).toISOString();
}

const PUBLISH_KIND: Record<string, { label: string; cls: string }> = {
  broadcast: { label: "广播", cls: "bg-cyan-900/40 text-cyan-300" },
  catalog: { label: "产品目录", cls: "bg-fuchsia-900/40 text-fuchsia-300" },
  daily: { label: "今日", cls: "bg-emerald-900/40 text-emerald-300" },
  scheduled: { label: "定时", cls: "bg-amber-900/40 text-amber-300" },
};

type LeadStatus = "new" | "contacted" | "won" | "lost";

const STATUS_META: Record<LeadStatus, { label: string; cls: string }> = {
  new: { label: "🆕 新", cls: "bg-sky-900/40 text-sky-300" },
  contacted: { label: "📞 已联系", cls: "bg-amber-900/40 text-amber-300" },
  won: { label: "💰 成交", cls: "bg-emerald-900/40 text-emerald-300" },
  lost: { label: "🗑 废弃", cls: "bg-slate-800 text-slate-500" },
};
const STATUS_ORDER: LeadStatus[] = ["new", "contacted", "won", "lost"];

interface LeadRow {
  id: string;
  t: string;
  firstSeen?: string;
  name: string;
  contact: string;
  interest: string;
  source: string;
  lang: string;
  status: LeadStatus;
  count: number;
  verified?: string;
}

interface CrmData {
  total: number;
  page: number;
  pageSize: number;
  pages: number;
  counts: Record<string, number>;
  rows: LeadRow[];
}

type TabId = "overview" | "growth" | "chat" | "content" | "crm" | "system";

const TABS: { id: TabId; label: string; Icon: LucideIcon }[] = [
  { id: "overview", label: "概览", Icon: LayoutDashboard },
  { id: "growth", label: "增长分析", Icon: TrendingUp },
  { id: "chat", label: "对话 & 知识库", Icon: MessagesSquare },
  { id: "content", label: "内容运营", Icon: Megaphone },
  { id: "crm", label: "CRM & 核销", Icon: Ticket },
  { id: "system", label: "系统", Icon: Activity },
];

/** Minimal dependency-free SVG sparkline. */
function Sparkline({
  data,
  color = "#22d3ee",
  w = 96,
  h = 28,
  full = false,
  markers,
  labels,
}: {
  data: number[];
  color?: string;
  w?: number;
  h?: number;
  full?: boolean;
  markers?: number[]; // day indices that had a content publish
  labels?: string[]; // optional per-point labels for hover tooltips
}) {
  if (!data || data.length < 2) return null;
  const max = Math.max(1, ...data);
  const min = Math.min(0, ...data);
  const span = max - min || 1;
  const step = w / (data.length - 1);
  const pts = data.map((v, i) => `${(i * step).toFixed(1)},${(h - ((v - min) / span) * h).toFixed(1)}`);
  const last = data[data.length - 1];
  const lastX = (data.length - 1) * step;
  const lastY = h - ((last - min) / span) * h;
  return (
    <svg
      width={full ? "100%" : w}
      height={h}
      viewBox={`0 0 ${w} ${h}`}
      preserveAspectRatio={full ? "none" : "xMidYMid meet"}
      className="overflow-visible"
    >
      {(markers ?? []).map((mi) =>
        mi >= 0 && mi < data.length ? (
          <line
            key={mi}
            x1={(mi * step).toFixed(1)}
            x2={(mi * step).toFixed(1)}
            y1={0}
            y2={h}
            stroke="#f59e0b"
            strokeWidth={1}
            strokeDasharray="2 2"
            opacity={0.55}
            vectorEffect="non-scaling-stroke"
          />
        ) : null
      )}
      <polyline points={pts.join(" ")} fill="none" stroke={color} strokeWidth={full ? 1 : 1.5} strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke" />
      <circle cx={lastX} cy={lastY} r={2} fill={color} />
      {full &&
        data.map((v, i) => (
          <rect
            key={i}
            x={(i * step - step / 2).toFixed(1)}
            y={0}
            width={step.toFixed(1)}
            height={h}
            fill="transparent"
            className="hover:fill-white/5"
          >
            <title>{`${labels?.[i] ?? `第 ${i + 1} 天`}：${v}`}</title>
          </rect>
        ))}
    </svg>
  );
}

/** Week-over-week delta badge. */
function Delta({ cur, prev }: { cur: number; prev: number }) {
  if (prev === 0 && cur === 0) return <span className="text-xs text-slate-600">—</span>;
  const pct = prev === 0 ? 100 : Math.round(((cur - prev) / prev) * 100);
  const up = cur >= prev;
  return (
    <span className={`inline-flex items-center gap-0.5 rounded px-1 text-[11px] font-medium ${up ? "bg-emerald-900/40 text-emerald-300" : "bg-rose-900/40 text-rose-300"}`}>
      {up ? <ArrowUpRight className="h-3 w-3" /> : <ArrowDownRight className="h-3 w-3" />}
      {Math.abs(pct)}%
    </span>
  );
}

function Kpi({
  label,
  value,
  hint,
  Icon,
  accent = "text-cyan-400",
  delta,
  spark,
  sparkColor,
}: {
  label: string;
  value: string | number;
  hint?: string;
  Icon: LucideIcon;
  accent?: string;
  delta?: { cur: number; prev: number };
  spark?: number[];
  sparkColor?: string;
}) {
  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-4 transition hover:border-slate-700">
      <div className="flex items-center justify-between">
        <span className="text-[13px] text-slate-400">{label}</span>
        <Icon className={`h-4 w-4 ${accent}`} />
      </div>
      <div className="mt-2 flex items-end justify-between gap-2">
        <div className="text-3xl font-bold tracking-tight text-white">{value}</div>
        {spark && spark.length > 1 && <Sparkline data={spark} color={sparkColor} />}
      </div>
      <div className="mt-1 flex items-center gap-2">
        {delta && <Delta cur={delta.cur} prev={delta.prev} />}
        {hint && <span className="text-xs text-slate-500">{hint}</span>}
      </div>
    </div>
  );
}

/** Conversion funnel: each step shows volume + step-over-step conversion %. */
function Funnel({ steps }: { steps: { label: string; value: number; color: string }[] }) {
  const top = Math.max(1, steps[0]?.value ?? 1);
  return (
    <div className="space-y-2.5">
      {steps.map((s, i) => {
        const pct = Math.round((s.value / top) * 100);
        const prev = i === 0 ? null : steps[i - 1].value;
        const conv = prev === null ? null : prev === 0 ? 0 : Math.round((s.value / prev) * 100);
        return (
          <div key={s.label} className="flex items-center gap-3">
            <span className="w-24 shrink-0 text-right text-xs text-slate-400">{s.label}</span>
            <div className="h-8 flex-1 overflow-hidden rounded-lg bg-slate-800/50">
              <div
                className={`flex h-full items-center rounded-lg bg-gradient-to-r ${s.color} px-2.5 text-xs font-bold text-slate-950`}
                style={{ width: `${Math.max(pct, 8)}%` }}
              >
                {s.value}
              </div>
            </div>
            <span className="w-12 shrink-0 text-right text-[11px] text-slate-500">
              {conv === null ? "100%" : `${conv}%`}
            </span>
          </div>
        );
      })}
    </div>
  );
}

/** 维度转化表：每行 维度值 | 会话数 | 转化数 | 转化率%，按会话量降序。 */
function ConvTable({
  title,
  rows,
  labels,
}: {
  title: string;
  rows: { key: string; sessions: number; convert: number; rate: number }[];
  labels?: Record<string, string>;
}) {
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-950/40 p-3">
      <div className="mb-2 text-[11px] text-slate-400">{title}</div>
      {rows.length === 0 ? (
        <div className="text-[11px] text-slate-600">暂无会话数据</div>
      ) : (
        <table className="w-full text-[11px]">
          <thead>
            <tr className="text-slate-500">
              <th className="text-left font-medium" />
              <th className="text-right font-medium">会话</th>
              <th className="text-right font-medium">转化</th>
              <th className="text-right font-medium">转化率</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.key} className="text-slate-300">
                <td className="py-0.5 text-left">{labels?.[r.key] ?? r.key}</td>
                <td className="py-0.5 text-right">{r.sessions}</td>
                <td className="py-0.5 text-right text-emerald-300">{r.convert}</td>
                <td className="py-0.5 text-right font-medium text-cyan-300">{r.rate}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function TaskTile({
  Icon,
  label,
  value,
  accent,
  onClick,
}: {
  Icon: LucideIcon;
  label: string;
  value: number;
  accent: string;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className="flex items-center justify-between rounded-xl border border-slate-800 bg-slate-950/40 p-3 text-left transition hover:border-slate-600"
    >
      <div className="flex items-center gap-2">
        <Icon className={`h-5 w-5 ${accent}`} />
        <span className="text-sm text-slate-300">{label}</span>
      </div>
      <span className={`text-2xl font-bold ${value > 0 ? "text-white" : "text-slate-600"}`}>{value}</span>
    </button>
  );
}

function SectionCard({
  title,
  Icon,
  accent = "text-slate-200",
  right,
  children,
}: {
  title: string;
  Icon?: LucideIcon;
  accent?: string;
  right?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/50 p-5">
      <div className="mb-4 flex items-center justify-between gap-2">
        <div className={`flex items-center gap-2 text-sm font-semibold ${accent}`}>
          {Icon && <Icon className="h-4 w-4" />}
          {title}
        </div>
        {right}
      </div>
      {children}
    </div>
  );
}

export default function AdminPage() {
  const [key, setKey] = useState("");
  const [stats, setStats] = useState<Stats | null>(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);
  const [kb, setKb] = useState<KbEntry[]>([]);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [savingQ, setSavingQ] = useState<string | null>(null);
  const [bcText, setBcText] = useState("");
  const [bcTarget, setBcTarget] = useState<BroadcastTarget>("channel");
  const [bcButton, setBcButton] = useState(true);
  const [bcMsg, setBcMsg] = useState("");
  const [bcSending, setBcSending] = useState(false);
  const [bcRunAt, setBcRunAt] = useState("");
  const [scheduled, setScheduled] = useState<ScheduledPost[]>([]);
  const [calView, setCalView] = useState<"week" | "list">("week");
  const [weekStart, setWeekStart] = useState<string>(() => weekStartKey(todayLocalKey()));
  const [dragId, setDragId] = useState<string | null>(null);
  const [dragOverDay, setDragOverDay] = useState<string | null>(null);
  const [templates, setTemplates] = useState<Template[]>([]);
  const [aiDrafts, setAiDrafts] = useState<DraftPost[]>([]);
  const [genning, setGenning] = useState(false);
  const [catTarget, setCatTarget] = useState<"channel" | "group" | "both">("channel");
  const [catLang, setCatLang] = useState<"zh" | "en">("zh");
  const [health, setHealth] = useState<HealthSnapshot | null>(null);
  const [alerts, setAlerts] = useState<AlertItem[]>([]);
  const [leadFilter, setLeadFilter] = useState<LeadStatus | "all">("all");
  const [redeemCode, setRedeemCode] = useState("");
  const [redeemMsg, setRedeemMsg] = useState("");
  const [redeeming, setRedeeming] = useState(false);
  const [codes, setCodes] = useState<
    { code: string; contact?: string; name?: string; lang?: string; issuedAt: string; expiresAt?: string; redeemed: boolean; redeemedAt?: string; userId: number }[]
  >([]);
  const [codesFilter, setCodesFilter] = useState<"all" | "unused" | "used">("all");
  const [unlockStats, setUnlockStats] = useState<{
    overall: { issued: number; redeemed: number; rate: number };
    byLang: { key: string; issued: number; redeemed: number; rate: number }[];
  } | null>(null);
  const [codesBusy, setCodesBusy] = useState(false);
  const [crmSearch, setCrmSearch] = useState("");
  const [tab, setTab] = useState<TabId>("overview");
  const [toast, setToast] = useState<{ msg: string; ok: boolean } | null>(null);
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  // Mini App 漏斗时间窗：0=全部，7/30/90 天。用 ref 让 30s 自动刷新定时器也能读到最新窗口。
  const [miWindow, setMiWindow] = useState(0);
  const miWindowRef = useRef(0);

  // ── server-paged CRM (dedicated /api/admin/leads endpoint) ──
  const [crmSort, setCrmSort] = useState<"lastSeen" | "firstSeen" | "count" | "name" | "status">("lastSeen");
  const [crmDir, setCrmDir] = useState<"asc" | "desc">("desc");
  const [crmFrom, setCrmFrom] = useState("");
  const [crmTo, setCrmTo] = useState("");
  const [crmPage, setCrmPage] = useState(1);
  const [crmData, setCrmData] = useState<CrmData | null>(null);
  const [crmLoading, setCrmLoading] = useState(false);
  const CRM_PAGE_SIZE = 20;

  function buildLeadQuery(extra?: Record<string, string>): string {
    const p = new URLSearchParams({
      status: leadFilter,
      q: crmSearch.trim(),
      from: crmFrom,
      to: crmTo,
      sort: crmSort,
      dir: crmDir,
      page: String(crmPage),
      pageSize: String(CRM_PAGE_SIZE),
      ...extra,
    });
    return p.toString();
  }

  async function loadLeads() {
    setCrmLoading(true);
    try {
      const res = await fetch(`/api/admin/leads?${buildLeadQuery()}`);
      if (res.ok) setCrmData(await res.json());
    } catch {
      /* ignore */
    } finally {
      setCrmLoading(false);
    }
  }

  function toggleSort(field: "lastSeen" | "firstSeen" | "count" | "name" | "status") {
    if (crmSort === field) {
      setCrmDir((d) => (d === "desc" ? "asc" : "desc"));
    } else {
      setCrmSort(field);
      setCrmDir("desc");
    }
    setCrmPage(1);
  }

  // Reload leads (debounced) whenever the CRM view or any query param changes.
  useEffect(() => {
    if (!stats || tab !== "crm") return;
    const id = window.setTimeout(() => void loadLeads(), 250);
    return () => window.clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stats, tab, leadFilter, crmSearch, crmFrom, crmTo, crmSort, crmDir, crmPage]);

  function showToast(msg: string, ok = true) {
    setToast({ msg, ok });
    window.setTimeout(() => setToast(null), 2600);
  }

  async function copyText(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      showToast("已复制：" + text);
    } catch {
      showToast("复制失败", false);
    }
  }

  // Build a t.me link when the contact looks like a Telegram handle/id, else null.
  function tgLink(contact: string): string | null {
    const c = (contact || "").trim();
    if (c.startsWith("@")) return `https://t.me/${c.slice(1)}`;
    if (/^tg:\/\/user\?id=\d+$/.test(c)) return c;
    if (/^\d{5,}$/.test(c)) return `tg://user?id=${c}`;
    return null;
  }

  // Export the full filtered set (not just the current page) via the export-mode endpoint.
  async function exportLeadsCsv() {
    let rows: LeadRow[] = crmData?.rows ?? [];
    try {
      const res = await fetch(`/api/admin/leads?${buildLeadQuery({ all: "1" })}`);
      if (res.ok) {
        const data = (await res.json()) as CrmData;
        rows = data.rows;
      }
    } catch {
      /* fall back to current page rows */
    }
    const head = ["时间", "称呼", "联系方式", "意向", "来源", "语言", "状态", "次数"];
    const esc = (v: string | number) => `"${String(v ?? "").replace(/"/g, '""')}"`;
    const lines = [head.map(esc).join(",")];
    for (const l of rows) {
      lines.push(
        [l.t, l.name, l.contact, l.interest, l.source, l.lang, STATUS_META[l.status].label, l.count]
          .map(esc)
          .join(","),
      );
    }
    const blob = new Blob(["\uFEFF" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `leads-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
    showToast(`已导出 ${rows.length} 条`);
  }

  async function logout() {
    try {
      await fetch("/api/admin/logout", { method: "POST" });
    } catch {
      // ignore
    }
    setKey("");
    setStats(null);
    setAutoRefresh(false);
  }

  // On mount, try the existing httpOnly session cookie silently.
  useEffect(() => {
    void load(true);
  }, []);

  // optional 30s auto-refresh while logged in
  useEffect(() => {
    if (!autoRefresh || !stats) return;
    const id = window.setInterval(() => void load(true), 30000);
    return () => window.clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoRefresh, stats]);

  async function doLogin() {
    setLoading(true);
    setErr("");
    try {
      const res = await fetch("/api/admin/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key }),
      });
      if (!res.ok) {
        setErr(res.status === 401 ? "口令错误" : "登录失败");
        return;
      }
      await load();
    } catch {
      setErr("网络错误");
    } finally {
      setLoading(false);
    }
  }

  async function load(silent = false) {
    setLoading(true);
    if (!silent) setErr("");
    try {
      const res = await fetch(`/api/admin/stats${miWindowRef.current ? `?days=${miWindowRef.current}` : ""}`);
      if (!res.ok) {
        // 401 on mount simply means "not logged in" -> show login card quietly.
        if (!silent && res.status !== 401) setErr("加载失败");
        setStats(null);
        return;
      }
      const data = await res.json();
      setStats(data);
      setLastUpdated(new Date());
      void loadKb("");
      void loadSchedule("");
      void loadTemplates("");
      void loadDrafts("");
      void loadHealth("");
      void loadCodes("");
    } catch {
      if (!silent) setErr("网络错误");
    } finally {
      setLoading(false);
    }
  }

  // 切换 Mini App 漏斗时间窗后立即重拉（ref 同步，确保自动刷新也用新窗口）。
  function applyMiWindow(v: number) {
    miWindowRef.current = v;
    setMiWindow(v);
    void load(true);
  }

  // 把漏斗/维度/卡点数字翻译成「一句话结论」，帮运营直接看懂下一步做什么。
  // 带样本量守卫：会话过少时只给「仅供参考」，不下硬结论，避免小样本误导。
  function miniDiagnostics(): { level: "warn" | "good" | "info"; text: string }[] {
    const m = stats?.miniapp;
    if (!m?.funnel) return [];
    const f = m.funnel;
    const out: { level: "warn" | "good" | "info"; text: string }[] = [];
    if (f.sessions < 10) out.push({ level: "info", text: `会话样本较少（${f.sessions}），以下结论仅供参考。` });
    if (f.sessions > 0) {
      const stages = [
        { name: "进入→产生兴趣", rate: f.rates.engaged },
        { name: "兴趣→高意向", rate: f.rates.intent },
        { name: "高意向→转化", rate: f.rates.convert },
      ];
      const worst = stages.reduce((a, b) => (b.rate < a.rate ? b : a));
      if (worst.rate < 60) out.push({ level: "warn", text: `最大流失在「${worst.name}」，仅 ${worst.rate}% 通过，优先优化此处。` });
    }
    const g = m.gateSteps ?? {};
    const vf = g.verify_fail ?? 0;
    const vo = g.verify_ok ?? 0;
    if (vf > 0 && vf >= vo) out.push({ level: "warn", text: `解锁校验失败（${vf}）不少于成功（${vo}），检查频道/群链接或加入门槛。` });
    if (m.leadFlow && m.leadFlow.start >= 5 && m.leadFlow.abandonRate > 50)
      out.push({ level: "warn", text: `留资放弃率 ${m.leadFlow.abandonRate}%，建议简化表单字段或补充信任背书。` });
    const best = (m.byLanding ?? []).filter((r) => r.sessions >= 5).sort((a, b) => b.rate - a.rate)[0];
    if (best && best.rate > 0)
      out.push({ level: "good", text: `落地视图「${MINIAPP_VIEW_LABELS[best.key] ?? best.key}」转化最高（${best.rate}%），建议深链优先导向。` });
    return out.slice(0, 4);
  }

  async function loadKb(k: string) {
    try {
      const res = await fetch(`/api/admin/kb`);
      if (res.ok) {
        const data = await res.json();
        setKb(data.entries ?? []);
      }
    } catch {
      /* ignore */
    }
  }

  async function saveKb(q: string, lang?: string) {
    const a = (drafts[q] ?? "").trim();
    if (!a) return;
    setSavingQ(q);
    try {
      const res = await fetch(`/api/admin/kb`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ q, a, lang }),
      });
      if (res.ok) {
        setDrafts((d) => {
          const n = { ...d };
          delete n[q];
          return n;
        });
        await loadKb(key);
      }
    } catch {
      /* ignore */
    } finally {
      setSavingQ(null);
    }
  }

  async function delKb(id: string) {
    try {
      const res = await fetch(`/api/admin/kb?id=${encodeURIComponent(id)}`, {
        method: "DELETE",
      });
      if (res.ok) await loadKb(key);
    } catch {
      /* ignore */
    }
  }

  async function changeLeadStatus(id: string, status: LeadStatus) {
    setStats((prev) =>
      prev
        ? { ...prev, recentLeads: prev.recentLeads.map((l) => (l.id === id ? { ...l, status } : l)) }
        : prev
    );
    setCrmData((prev) =>
      prev ? { ...prev, rows: prev.rows.map((l) => (l.id === id ? { ...l, status } : l)) } : prev
    );
    try {
      await fetch(`/api/admin/lead-status`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id, status }),
      });
    } catch {
      /* optimistic; reload to resync if needed */
    }
    // resync counts/pagination after the write settles
    void loadLeads();
  }

  async function loadSchedule(k: string) {
    try {
      const res = await fetch(`/api/admin/schedule`);
      if (res.ok) setScheduled((await res.json()).scheduled ?? []);
    } catch {
      /* ignore */
    }
  }

  async function loadTemplates(k: string) {
    try {
      const res = await fetch(`/api/admin/templates`);
      if (res.ok) setTemplates((await res.json()).templates ?? []);
    } catch {
      /* ignore */
    }
  }

  async function schedulePost() {
    const text = bcText.trim();
    if (!text || !bcRunAt) {
      setBcMsg("❌ 需要内容和发布时间");
      return;
    }
    setBcSending(true);
    setBcMsg("");
    try {
      const res = await fetch(`/api/admin/schedule`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, target: bcTarget, withButton: bcButton, runAt: new Date(bcRunAt).toISOString() }),
      });
      if (res.ok) {
        setBcMsg("✅ 已加入定时队列");
        setBcText("");
        setBcRunAt("");
        await loadSchedule(key);
      } else {
        setBcMsg("❌ 定时失败");
      }
    } catch {
      setBcMsg("❌ 网络错误");
    } finally {
      setBcSending(false);
    }
  }

  async function delScheduled(id: string) {
    try {
      await fetch(`/api/admin/schedule?id=${encodeURIComponent(id)}`, { method: "DELETE" });
      await loadSchedule(key);
    } catch {
      /* ignore */
    }
  }

  // Drag-and-drop reschedule: move a pending post to another calendar day (keeps time-of-day).
  async function reschedulePost(id: string, dayKey: string) {
    const post = scheduled.find((p) => p.id === id);
    if (!post || post.status !== "pending") return;
    if (localDayKey(post.runAt) === dayKey) return;
    const newRunAt = moveToDayKeepTime(post.runAt, dayKey);
    setScheduled((prev) => prev.map((p) => (p.id === id ? { ...p, runAt: newRunAt } : p)));
    try {
      const res = await fetch(`/api/admin/schedule`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id, runAt: newRunAt }),
      });
      if (res.ok) showToast(`已改期至 ${dayKey} ${localHM(newRunAt)}`);
      else showToast("改期失败", false);
    } catch {
      showToast("网络错误", false);
    }
    void loadSchedule(key);
  }

  async function saveTemplate() {
    const text = bcText.trim();
    if (!text) {
      setBcMsg("❌ 模板内容为空");
      return;
    }
    const name = window.prompt("模板名称：");
    if (!name) return;
    try {
      const res = await fetch(`/api/admin/templates`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, text, target: bcTarget, withButton: bcButton }),
      });
      if (res.ok) await loadTemplates(key);
    } catch {
      /* ignore */
    }
  }

  function applyTemplate(id: string) {
    const tpl = templates.find((t) => t.id === id);
    if (!tpl) return;
    setBcText(tpl.text);
    setBcTarget(tpl.target);
    setBcButton(tpl.withButton);
  }

  async function delTemplate(id: string) {
    try {
      await fetch(`/api/admin/templates?id=${encodeURIComponent(id)}`, { method: "DELETE" });
      await loadTemplates(key);
    } catch {
      /* ignore */
    }
  }

  async function loadHealth(k: string) {
    try {
      const res = await fetch(`/api/admin/health-check`);
      if (res.ok) {
        const data = await res.json();
        setHealth(data.health ?? null);
        setAlerts(data.alerts ?? []);
      }
    } catch {
      /* ignore */
    }
  }

  async function healthDrill(kind: "degrade" | "digest") {
    const q = kind === "degrade" ? "simulate=degrade" : "digest=1";
    try {
      const res = await fetch(`/api/admin/health-check?${q}`, { method: "POST" });
      if (res.ok) {
        showToast(kind === "degrade" ? "已演练降级告警（看管理员私聊）" : "已发送健康日报（看管理员私聊）");
        setTimeout(() => void loadHealth(""), 500);
      } else {
        showToast("演练失败", false);
      }
    } catch {
      showToast("网络错误", false);
    }
  }

  async function loadDrafts(k: string) {
    try {
      const res = await fetch(`/api/admin/daily`);
      if (res.ok) setAiDrafts((await res.json()).drafts ?? []);
    } catch {
      /* ignore */
    }
  }

  async function genDaily() {
    setGenning(true);
    setBcMsg("");
    try {
      const res = await fetch(`/api/admin/daily`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const data = await res.json();
      if (data.ok && data.draft) {
        setBcText(data.draft.text);
        setBcTarget("channel");
        setBcButton(true);
        setBcMsg(`✨ 已生成：${data.draft.theme ?? ""}（可编辑后发布/定时）`);
        await loadDrafts(key);
      } else {
        setBcMsg("❌ 生成失败（检查 DeepSeek key）");
      }
    } catch {
      setBcMsg("❌ 网络错误");
    } finally {
      setGenning(false);
    }
  }

  async function publishCatalog() {
    const where = catTarget === "channel" ? "频道" : catTarget === "group" ? "群" : "频道+群";
    if (!confirm(`把官网六大产品（${catLang === "zh" ? "中文" : "英文"}）图文帖发布到${where}？会自动替换上次发布的目录。`)) return;
    setGenning(true);
    setBcMsg("正在发布产品目录…");
    try {
      const res = await fetch(`/api/admin/publish-catalog`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target: catTarget, lang: catLang }),
      });
      const data = await res.json();
      setBcMsg(data.ok ? `✅ 已发布 ${data.count} 条产品帖到${where}` : `⚠️ 部分失败：${JSON.stringify(data.results)}`);
    } catch {
      setBcMsg("❌ 网络错误");
    } finally {
      setGenning(false);
    }
  }

  async function publishDailyNow() {
    if (!confirm("生成今日 AI 选题并带图发布到频道？")) return;
    setGenning(true);
    setBcMsg("正在生成并发布今日选题…");
    try {
      const res = await fetch(`/api/admin/daily`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ publish: catTarget, image: true }),
      });
      const data = await res.json();
      setBcMsg(data.published && data.ok ? `✅ 已带图发布今日选题：${data.theme ?? ""}` : `⚠️ 失败：${JSON.stringify(data.results ?? data.error)}`);
    } catch {
      setBcMsg("❌ 网络错误");
    } finally {
      setGenning(false);
    }
  }

  function applyDraft(id: string) {
    const d = aiDrafts.find((x) => x.id === id);
    if (!d) return;
    setBcText(d.text);
    setBcTarget("channel");
    setBcButton(true);
  }

  async function delDraft(id: string) {
    try {
      await fetch(`/api/admin/daily?id=${encodeURIComponent(id)}`, { method: "DELETE" });
      await loadDrafts(key);
    } catch {
      /* ignore */
    }
  }

  async function loadCodes(k: string) {
    try {
      const res = await fetch(`/api/admin/redeem`);
      if (res.ok) {
        const data = await res.json();
        setCodes(Array.isArray(data.codes) ? data.codes : []);
        setUnlockStats(data.stats ?? null);
      }
    } catch {
      /* ignore */
    }
  }

  async function extendOne(code: string, days = 7) {
    try {
      const res = await fetch(`/api/admin/redeem`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code, days }),
      });
      if (res.ok) {
        showToast(`已延长 ${code} ${days} 天`);
        void loadCodes("");
      } else {
        showToast("延长失败（可能已核销）", false);
      }
    } catch {
      showToast("网络错误", false);
    }
  }

  async function extendAllPending(days = 7) {
    if (!window.confirm(`将所有未核销折扣码有效期延长 ${days} 天（过期码也会被复活）？`)) return;
    setCodesBusy(true);
    try {
      const res = await fetch(`/api/admin/redeem`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scope: "unredeemed", days }),
      });
      const data = await res.json();
      if (res.ok) {
        showToast(`已延长 ${data.extended ?? 0} 个折扣码`);
        void loadCodes("");
      } else {
        showToast("批量延长失败", false);
      }
    } catch {
      showToast("网络错误", false);
    } finally {
      setCodesBusy(false);
    }
  }

  async function voidExpiredCodes() {
    const expiredN = codes.filter((c) => !c.redeemed && !!c.expiresAt && Date.now() > Date.parse(c.expiresAt)).length;
    if (expiredN === 0) {
      showToast("没有过期码需要清理");
      return;
    }
    if (!window.confirm(`确认清理 ${expiredN} 个已过期且未核销的折扣码？此操作不可撤销。`)) return;
    setCodesBusy(true);
    try {
      const res = await fetch(`/api/admin/redeem`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scope: "expired" }),
      });
      const data = await res.json();
      if (res.ok) {
        showToast(`已清理 ${data.removed ?? 0} 个过期码`);
        void loadCodes("");
      } else {
        showToast("清理失败", false);
      }
    } catch {
      showToast("网络错误", false);
    } finally {
      setCodesBusy(false);
    }
  }

  async function redeem() {
    const code = redeemCode.trim();
    if (!code) return;
    setRedeeming(true);
    setRedeemMsg("");
    try {
      const res = await fetch(`/api/admin/redeem`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code }),
      });
      const data = await res.json();
      if (data.ok) {
        const used = data.redeemedAt ? `（核销时间 ${String(data.redeemedAt).slice(5, 16).replace("T", " ")}）` : "";
        const tag = data.alreadyRedeemed ? "（此前已核销）" : "";
        setRedeemMsg(`✅ 有效：${data.contact ?? data.tg_user_id} ${used}${tag}`);
        void loadCodes("");
      } else if (res.status === 410) {
        setRedeemMsg(`⏰ 已过期${data.expiresAt ? `（${String(data.expiresAt).slice(0, 10)}）` : ""}`);
      } else {
        setRedeemMsg(res.status === 404 ? "❌ 无此折扣码" : "❌ 校验失败");
      }
    } catch {
      setRedeemMsg("❌ 网络错误");
    } finally {
      setRedeeming(false);
    }
  }

  async function redeemOne(code: string) {
    try {
      const res = await fetch(`/api/admin/redeem`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code }),
      });
      if (res.ok) {
        showToast(`已核销 ${code}`);
        void loadCodes("");
      } else if (res.status === 410) {
        showToast("该码已过期", false);
        void loadCodes("");
      } else {
        showToast("核销失败", false);
      }
    } catch {
      showToast("网络错误", false);
    }
  }

  async function broadcast() {
    const text = bcText.trim();
    if (!text) return;
    setBcSending(true);
    setBcMsg("");
    try {
      const res = await fetch(`/api/admin/broadcast`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, target: bcTarget, withButton: bcButton }),
      });
      const data = await res.json();
      if (data.ok) {
        setBcMsg("✅ 已发送");
        setBcText("");
      } else {
        const errs = (data.results ?? []).filter((r: { ok: boolean }) => !r.ok)
          .map((r: { chat: string; error?: string }) => `${r.chat}: ${r.error ?? "失败"}`).join("；");
        setBcMsg(`❌ ${errs || data.error || "发送失败（确认 Bot 是频道/群管理员）"}`);
      }
    } catch {
      setBcMsg("❌ 网络错误");
    } finally {
      setBcSending(false);
    }
  }

  const crmRows = crmData?.rows ?? [];

  const calToday = todayLocalKey();
  const calDays = Array.from({ length: 7 }, (_, i) => addDaysKey(weekStart, i));
  const pendingCount = scheduled.filter((p) => p.status === "pending").length;

  return (
    <div className="min-h-screen bg-slate-950 text-slate-200">
      {toast && (
        <div
          className={
            "fixed bottom-5 left-1/2 z-50 flex -translate-x-1/2 items-center gap-2 rounded-xl px-4 py-2.5 text-sm font-medium shadow-xl " +
            (toast.ok ? "bg-emerald-500 text-slate-950" : "bg-rose-500 text-white")
          }
        >
          {toast.ok ? <CheckCircle2 className="h-4 w-4" /> : <XCircle className="h-4 w-4" />}
          {toast.msg}
        </div>
      )}

      {!stats ? (
        <div className="mx-auto flex min-h-screen max-w-sm flex-col justify-center px-6">
          <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-7">
            <div className="mb-1 flex items-center gap-2">
              <ShieldCheck className="h-6 w-6 text-cyan-400" />
              <span className="text-lg font-bold text-white">无界科技 · 控制台</span>
            </div>
            <p className="mb-5 text-xs text-slate-500">输入管理口令进入运营后台</p>
            <div className="relative">
              <Lock className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500" />
              <input
                type="password"
                value={key}
                onChange={(e) => setKey(e.target.value)}
                placeholder="管理口令"
                className="w-full rounded-xl border border-slate-700 bg-slate-950 py-2.5 pl-9 pr-3 text-sm outline-none focus:border-cyan-500"
                onKeyDown={(e) => e.key === "Enter" && doLogin()}
              />
            </div>
            <button
              onClick={doLogin}
              disabled={loading || !key}
              className="mt-3 w-full rounded-xl bg-cyan-500 py-2.5 text-sm font-semibold text-slate-950 disabled:opacity-50"
            >
              {loading ? "登录中…" : "进入后台"}
            </button>
            {err && <p className="mt-3 text-center text-sm text-rose-400">{err}</p>}
          </div>
        </div>
      ) : (
        <>
          <header className="sticky top-0 z-40 border-b border-slate-800 bg-slate-950/85 backdrop-blur">
            <div className="mx-auto max-w-6xl px-5">
              <div className="flex h-14 items-center justify-between gap-3">
                <div className="flex items-center gap-2">
                  <span className="text-base font-bold text-white">无界科技 · 控制台</span>
                  {health && (
                    <span
                      className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${
                        health.healthy ? "bg-emerald-900/50 text-emerald-300" : "bg-rose-900/50 text-rose-300"
                      }`}
                    >
                      {health.healthy ? "● 正常" : "● 降级"}
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-2">
                  {lastUpdated && (
                    <span className="hidden text-[11px] text-slate-500 sm:inline">
                      更新 {lastUpdated.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
                    </span>
                  )}
                  <label className="flex items-center gap-1 text-[11px] text-slate-400">
                    <input type="checkbox" checked={autoRefresh} onChange={(e) => setAutoRefresh(e.target.checked)} />
                    自动
                  </label>
                  <button
                    onClick={() => load()}
                    disabled={loading}
                    className="flex items-center gap-1 rounded-lg border border-slate-700 px-2.5 py-1.5 text-xs text-slate-300 hover:border-cyan-500 disabled:opacity-50"
                  >
                    <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
                    刷新
                  </button>
                  <button
                    onClick={logout}
                    className="flex items-center gap-1 rounded-lg border border-slate-700 px-2.5 py-1.5 text-xs text-slate-400 hover:border-rose-500 hover:text-rose-300"
                  >
                    <LogOut className="h-3.5 w-3.5" />
                    登出
                  </button>
                </div>
              </div>
              <nav className="flex gap-1 overflow-x-auto">
                {TABS.map(({ id, label, Icon }) => (
                  <button
                    key={id}
                    onClick={() => setTab(id)}
                    className={`flex shrink-0 items-center gap-1.5 border-b-2 px-3 py-2.5 text-sm font-medium transition ${
                      tab === id ? "border-cyan-400 text-cyan-300" : "border-transparent text-slate-400 hover:text-slate-200"
                    }`}
                  >
                    <Icon className="h-4 w-4" />
                    {label}
                  </button>
                ))}
              </nav>
            </div>
          </header>

          <main className="mx-auto max-w-6xl space-y-5 px-5 py-6">
            {health && !health.healthy && (
              <div className="rounded-xl border border-rose-600/50 bg-rose-950/30 p-3 text-sm text-rose-200">
                🚨 <b>服务降级</b>：{health.reasons.join("、")}
              </div>
            )}

            {tab === "overview" && (
              <div className="space-y-5">
                <SectionCard title="今日待办" Icon={ListChecks} accent="text-amber-300">
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                    <TaskTile
                      Icon={ClipboardCheck}
                      label="待核销折扣码"
                      value={stats.unlocks?.pending ?? Math.max(0, (stats.unlocks?.issued ?? 0) - (stats.unlocks?.redeemed ?? 0))}
                      accent="text-violet-300"
                      onClick={() => setTab("crm")}
                    />
                    <TaskTile
                      Icon={UserPlus}
                      label="今日新留资"
                      value={stats.series?.leads?.[(stats.series?.leads?.length ?? 1) - 1] ?? 0}
                      accent="text-emerald-300"
                      onClick={() => {
                        setLeadFilter("all");
                        setCrmSearch("");
                        setTab("crm");
                      }}
                    />
                    <TaskTile
                      Icon={Ticket}
                      label="待跟进（新）"
                      value={stats.statusCounts?.new ?? 0}
                      accent="text-sky-300"
                      onClick={() => {
                        setLeadFilter("new");
                        setCrmSearch("");
                        setTab("crm");
                      }}
                    />
                  </div>
                </SectionCard>

                <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
                  <Kpi label="浏览量 PV" value={stats.totals.pageviews} hint="近14天趋势" Icon={TrendingUp} delta={stats.wow?.pageviews} spark={stats.series?.pv} sparkColor="#22d3ee" />
                  <Kpi label="CTA 点击" value={stats.totals.ctaClicks} hint="周环比" Icon={Activity} accent="text-fuchsia-400" delta={stats.wow?.ctaClicks} spark={stats.series?.cta} sparkColor="#e879f9" />
                  <Kpi label="AI 对话" value={stats.totals.chats ?? 0} hint="问答次数" Icon={MessagesSquare} accent="text-sky-400" />
                  <Kpi label="留资(去重)" value={stats.totals.leads} hint="周环比" Icon={Ticket} accent="text-emerald-400" delta={stats.wow?.leads} spark={stats.series?.leads} sparkColor="#34d399" />
                  <Kpi label="留资转化率" value={`${stats.totals.convRate}%`} hint="留资 / PV" Icon={TrendingUp} accent="text-amber-400" />
                  <Kpi label="解锁领码" value={stats.unlocks?.issued ?? 0} hint={`核销 ${stats.unlocks?.redeemed ?? 0}`} Icon={Ticket} accent="text-violet-400" />
                </div>

                <SectionCard title="转化漏斗（PV → 成交）" Icon={TrendingUp} accent="text-cyan-300">
                  <Funnel
                    steps={[
                      { label: "浏览 PV", value: stats.totals.pageviews, color: "from-cyan-400 to-cyan-500" },
                      { label: "CTA 点击", value: stats.totals.ctaClicks, color: "from-sky-400 to-sky-500" },
                      { label: "AI 对话", value: stats.totals.chats ?? 0, color: "from-blue-400 to-blue-500" },
                      { label: "留资", value: stats.totals.leads, color: "from-emerald-400 to-emerald-500" },
                      { label: "解锁领码", value: stats.unlocks?.issued ?? 0, color: "from-violet-400 to-violet-500" },
                      { label: "核销", value: stats.unlocks?.redeemed ?? 0, color: "from-fuchsia-400 to-fuchsia-500" },
                    ]}
                  />
                </SectionCard>

                <div className="grid gap-3 md:grid-cols-2">
                  <Bars title="留资来源（web / miniapp）" data={stats.leadsBySource} />
                  <Bars title="CTA 点击位置" data={stats.ctaByWhere} />
                </div>
              </div>
            )}

            {tab === "growth" && (
              <div className="space-y-3">
                {stats.series && (
                  <SectionCard title="近 14 天趋势" Icon={TrendingUp} accent="text-cyan-300">
                    <div className="space-y-4">
                      {[
                        { label: "浏览量 PV", data: stats.series.pv, color: "#22d3ee", wow: stats.wow?.pageviews },
                        { label: "CTA 点击", data: stats.series.cta, color: "#e879f9", wow: stats.wow?.ctaClicks },
                        { label: "每日留资", data: stats.series.leads, color: "#34d399", wow: stats.wow?.leads },
                      ].map((m) => (
                        <div key={m.label} className="flex items-center gap-4">
                          <div className="w-24 shrink-0">
                            <div className="text-xs text-slate-400">{m.label}</div>
                            <div className="mt-0.5 flex items-center gap-1.5">
                              <span className="text-lg font-bold text-white">{m.data.reduce((a, b) => a + b, 0)}</span>
                              {m.wow && <Delta cur={m.wow.cur} prev={m.wow.prev} />}
                            </div>
                          </div>
                          <div className="flex-1">
                            <Sparkline data={m.data} color={m.color} w={520} h={44} full markers={stats.publishDays} labels={stats.series?.days} />
                          </div>
                        </div>
                      ))}
                    </div>
                    <div className="mt-2 flex items-center justify-between text-[11px] text-slate-600">
                      <span>
                        {stats.series.days[0]?.slice(5)} ~ {stats.series.days[stats.series.days.length - 1]?.slice(5)} · 每日计数
                      </span>
                      {(stats.publishDays?.length ?? 0) > 0 && (
                        <span className="inline-flex items-center gap-1 text-amber-500/80">
                          <span className="inline-block h-3 w-px border-l border-dashed border-amber-500" /> 内容发布日
                        </span>
                      )}
                    </div>
                  </SectionCard>
                )}
                <div className="grid gap-3 md:grid-cols-2">
                  <Bars title="留资来源（web / miniapp）" data={stats.leadsBySource} />
                  <Bars title="留资意向分布" data={stats.leadsByInterest} />
                  <Bars title="CTA 点击位置" data={stats.ctaByWhere} />
                  <Bars title="每日留资" data={stats.leadsByDay} />
                </div>

                {stats.miniapp && (
                  <SectionCard title="小程序漏斗（Mini App · Telegram）" Icon={TrendingUp} accent="text-violet-300">
                    <div className="mb-3 flex items-center justify-between gap-2">
                      <span className="text-[11px] text-slate-500">
                        {stats.miniapp.dropped ? `已过滤异常高频会话 ${stats.miniapp.dropped} 个（防刷）` : "会话漏斗/维度/卡点按所选时间窗统计"}
                      </span>
                      <div className="flex gap-1">
                        {[
                          { v: 0, label: "全部" },
                          { v: 7, label: "近7天" },
                          { v: 30, label: "近30天" },
                          { v: 90, label: "近90天" },
                        ].map((o) => (
                          <button
                            key={o.v}
                            onClick={() => applyMiWindow(o.v)}
                            className={`rounded-md px-2 py-0.5 text-[11px] transition-colors ${
                              miWindow === o.v ? "bg-violet-500 text-white" : "bg-slate-800 text-slate-400 hover:text-slate-200"
                            }`}
                          >
                            {o.label}
                          </button>
                        ))}
                      </div>
                    </div>
                    {stats.miniapp.opens === 0 ? (
                      <div className="py-3 text-center text-[11px] text-slate-600">
                        {miWindow > 0
                          ? `近 ${miWindow} 天内暂无小程序数据，可切回「全部」查看历史。`
                          : "暂无小程序埋点数据。用户在 Telegram 打开小程序、切视图、点 CTA、留资后即会出现。"}
                      </div>
                    ) : (
                      <div className="space-y-4">
                        {(() => {
                          const ins = miniDiagnostics();
                          return ins.length > 0 ? (
                            <div className="space-y-1">
                              {ins.map((it, i) => (
                                <div
                                  key={i}
                                  className={`rounded-lg border px-2.5 py-1.5 text-[11px] ${
                                    it.level === "warn"
                                      ? "border-amber-500/30 bg-amber-500/10 text-amber-200"
                                      : it.level === "good"
                                        ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-200"
                                        : "border-slate-700 bg-slate-800/40 text-slate-400"
                                  }`}
                                >
                                  {it.level === "warn" ? "⚠ " : it.level === "good" ? "✓ " : "ℹ "}
                                  {it.text}
                                </div>
                              ))}
                            </div>
                          ) : null;
                        })()}
                        {stats.miniapp.series && (
                          <div className="grid grid-cols-3 gap-3">
                            {[
                              { label: "进入", data: stats.miniapp.series.open, color: "#a78bfa", wow: stats.miniapp.wow?.opens },
                              { label: "CTA", data: stats.miniapp.series.cta, color: "#22d3ee", wow: stats.miniapp.wow?.cta },
                              { label: "留资", data: stats.miniapp.series.lead, color: "#34d399", wow: stats.miniapp.wow?.leads },
                            ].map((m) => (
                              <div key={m.label} className="rounded-xl border border-slate-800 bg-slate-950/40 p-3">
                                <div className="text-[11px] text-slate-400">{m.label}（近14天）</div>
                                <div className="mt-0.5 flex items-center gap-1.5">
                                  <span className="text-lg font-bold text-white">{m.data.reduce((a, b) => a + b, 0)}</span>
                                  {m.wow && <Delta cur={m.wow.cur} prev={m.wow.prev} />}
                                </div>
                                <div className="mt-1">
                                  <Sparkline data={m.data} color={m.color} w={160} h={28} full labels={stats.series?.days} />
                                </div>
                              </div>
                            ))}
                          </div>
                        )}
                        {stats.miniapp.funnel && stats.miniapp.funnel.sessions > 0 ? (
                          <div>
                            <div className="mb-2 flex items-center justify-between">
                              <span className="text-[11px] text-slate-400">会话级漏斗（按 sid 串联）</span>
                              <span className="text-[11px] text-emerald-300">端到端转化 {stats.miniapp.funnel.rates.overall}%</span>
                            </div>
                            <Funnel
                              steps={[
                                { label: "进入会话", value: stats.miniapp.funnel.sessions, color: "from-violet-400 to-violet-500" },
                                { label: "产生兴趣", value: stats.miniapp.funnel.engaged, color: "from-sky-400 to-sky-500" },
                                { label: "高意向", value: stats.miniapp.funnel.intent, color: "from-cyan-400 to-cyan-500" },
                                { label: "转化·留资/解锁", value: stats.miniapp.funnel.convert, color: "from-emerald-400 to-emerald-500" },
                              ]}
                            />
                            <div className="mt-1.5 text-[10px] text-slate-600">
                              互动量（事件计数，含历史无会话数据）：CTA {stats.miniapp.cta} · AI 对话 {stats.miniapp.chats ?? 0} · 留资 {stats.miniapp.leads} · 解锁 {stats.miniapp.unlocks}
                            </div>
                          </div>
                        ) : (
                          <Funnel
                            steps={[
                              { label: "进入小程序", value: stats.miniapp.opens, color: "from-violet-400 to-violet-500" },
                              { label: "点击 CTA", value: stats.miniapp.cta, color: "from-cyan-400 to-cyan-500" },
                              { label: "留资", value: stats.miniapp.leads, color: "from-emerald-400 to-emerald-500" },
                              { label: "解锁领码", value: stats.miniapp.unlocks, color: "from-fuchsia-400 to-fuchsia-500" },
                            ]}
                          />
                        )}
                        <div className="grid gap-3 md:grid-cols-3">
                          <Bars title="各视图浏览热度" data={relabel(stats.miniapp.viewVisits, MINIAPP_VIEW_LABELS)} />
                          <Bars title="各视图 CTA 点击" data={relabel(stats.miniapp.ctaByView, MINIAPP_VIEW_LABELS)} />
                          <Bars title="进入来源（深链/直达）" data={stats.miniapp.openBySource} />
                        </div>
                        {(stats.miniapp.byLanding?.length || stats.miniapp.bySource?.length) ? (
                          <div className="mt-3 grid gap-3 md:grid-cols-2">
                            <ConvTable title="落地视图 → 会话转化率" rows={stats.miniapp.byLanding ?? []} labels={MINIAPP_VIEW_LABELS} />
                            <ConvTable title="进入来源 → 会话转化率" rows={stats.miniapp.bySource ?? []} />
                          </div>
                        ) : null}
                        {(Object.keys(stats.miniapp.gateSteps ?? {}).length > 0 || (stats.miniapp.leadFlow?.start ?? 0) > 0) ? (
                          <div className="mt-3 grid gap-3 md:grid-cols-2">
                            <Bars title="解锁卡点（关注/进群/校验）" data={relabel(stats.miniapp.gateSteps ?? {}, GATE_LABELS)} />
                            <div className="rounded-xl border border-slate-800 bg-slate-950/40 p-3">
                              <div className="mb-2 text-[11px] text-slate-400">留资放弃率</div>
                              <div className="flex items-end gap-3">
                                <div>
                                  <div className="text-lg font-semibold text-slate-200">{stats.miniapp.leadFlow?.start ?? 0}</div>
                                  <div className="text-[10px] text-slate-500">开始填写</div>
                                </div>
                                <div className="pb-1 text-slate-600">→</div>
                                <div>
                                  <div className="text-lg font-semibold text-emerald-300">{stats.miniapp.leadFlow?.done ?? 0}</div>
                                  <div className="text-[10px] text-slate-500">提交成功</div>
                                </div>
                                <div className="ml-auto text-right">
                                  <div className="text-lg font-semibold text-rose-300">{stats.miniapp.leadFlow?.abandonRate ?? 0}%</div>
                                  <div className="text-[10px] text-slate-500">放弃率</div>
                                </div>
                              </div>
                              <div className="mt-2 text-[10px] text-slate-600">开始填写联系方式但未成功提交的会话占比，越高说明表单/信任环节流失越大</div>
                            </div>
                          </div>
                        ) : null}
                      </div>
                    )}
                  </SectionCard>
                )}
              </div>
            )}

            {tab === "system" && health && (
          <div className="mt-4 rounded-xl border border-slate-700/60 bg-slate-900/40 p-4">
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <div className="text-sm font-semibold text-slate-200">系统状态</div>
              <div className="flex items-center gap-1.5">
                <button
                  onClick={() => healthDrill("digest")}
                  className="rounded-md border border-sky-700/50 px-2 py-0.5 text-[11px] text-sky-300 hover:border-sky-500"
                >
                  发送健康日报
                </button>
                <button
                  onClick={() => healthDrill("degrade")}
                  className="rounded-md border border-amber-700/50 px-2 py-0.5 text-[11px] text-amber-300 hover:border-amber-500"
                >
                  演练降级告警
                </button>
                <span
                  className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${
                    health.healthy ? "bg-emerald-900/50 text-emerald-300" : "bg-rose-900/50 text-rose-300"
                  }`}
                >
                  {health.healthy ? "● 正常" : "● 降级"}
                </span>
              </div>
            </div>
            <div className="grid grid-cols-2 gap-2 text-[11px] md:grid-cols-4">
              <HealthCell label="存储" ok={health.checks.storage?.ok} text={health.checks.storage?.ok ? "可写" : "异常"} />
              <HealthCell
                label="Telegram"
                ok={health.checks.telegram?.ok}
                text={health.checks.telegram?.ok ? `@${health.checks.telegram?.bot ?? "ok"}` : "不可达"}
              />
              <HealthCell
                label="DeepSeek 熔断"
                ok={health.checks.deepseek?.circuit === "closed"}
                text={`${health.checks.deepseek?.circuit ?? "?"}（跳闸${health.checks.deepseek?.totalTrips ?? 0}）`}
              />
              <HealthCell
                label="今日用量"
                ok={(health.checks.usage?.remaining ?? 1) > 0}
                text={`${health.checks.usage?.count ?? 0}/${health.checks.usage?.cap ?? 0}`}
              />
            </div>
            {alerts.length > 0 && (
              <div className="mt-3 border-t border-slate-800 pt-2">
                <div className="mb-1 text-[11px] text-slate-500">最近告警（{alerts.length}）</div>
                <div className="space-y-1">
                  {alerts.slice(0, 6).map((a, i) => (
                    <div key={i} className="flex items-center justify-between text-[11px]">
                      <span className={a.kind === "recover" ? "text-emerald-300" : a.kind === "digest" ? "text-sky-300" : "text-rose-300"}>
                        {a.kind === "recover" ? "✅ 恢复" : a.kind === "digest" ? "📊 日报" : "🚨 降级"}
                        {a.kind === "degrade" && a.consec ? `（连续 ${a.consec} 次）` : ""} · {a.reasons.join("、")}
                      </span>
                      <span className="text-slate-600">
                        {String(a.t).slice(5, 16).replace("T", " ")}
                        {!a.delivered && " · 未推送"}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
            )}

            {tab === "chat" && (
              <>

            {stats.chatCluster && stats.chatCluster.total > 0 && (
              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <div className="text-sm font-semibold text-slate-200">问题聚类 · 运营抓手</div>
                  <div className="text-xs text-slate-500">
                    共 {stats.chatCluster.total} 问 · 已归类 {stats.chatCluster.coverage}%
                  </div>
                </div>
                <div className="grid gap-3 md:grid-cols-2">
                  <div className="rounded-xl border border-slate-700 bg-slate-900 p-4">
                    <div className="mb-3 text-sm font-semibold text-slate-200">热门问题主题（Top）</div>
                    <div className="space-y-2.5">
                      {stats.chatCluster.topics.map((t) => {
                        const max = Math.max(1, ...stats.chatCluster!.topics.map((x) => x.count));
                        return (
                          <div key={t.id}>
                            <div className="flex items-center gap-2">
                              <span className="w-36 shrink-0 truncate text-xs text-slate-300" title={t.label}>
                                {t.label}
                              </span>
                              <div className="h-4 flex-1 overflow-hidden rounded bg-slate-800">
                                <div className="h-full rounded bg-emerald-500" style={{ width: `${(t.count / max) * 100}%` }} />
                              </div>
                              <span className="w-10 shrink-0 text-right text-xs tabular-nums text-slate-300">{t.count}</span>
                            </div>
                            {t.samples.length > 0 && (
                              <div className="ml-[9.5rem] mt-1 text-[11px] text-slate-500 truncate" title={t.samples.join(" · ")}>
                                {t.samples.join(" · ")}
                              </div>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  </div>

                  <div className="rounded-xl border border-slate-700 bg-slate-900 p-4">
                    <div className="mb-3 text-sm font-semibold text-slate-200">客户语种分布（按提问文字判定）</div>
                    <div className="space-y-2">
                      {stats.chatCluster.langs.map((l) => {
                        const max = Math.max(1, ...stats.chatCluster!.langs.map((x) => x.count));
                        return (
                          <div key={l.lang} className="flex items-center gap-2">
                            <span className="w-24 shrink-0 truncate text-xs text-slate-400">
                              {LANG_NAMES[l.lang] ?? l.lang}
                            </span>
                            <div className="h-4 flex-1 overflow-hidden rounded bg-slate-800">
                              <div className="h-full rounded bg-violet-500" style={{ width: `${(l.count / max) * 100}%` }} />
                            </div>
                            <span className="w-10 shrink-0 text-right text-xs tabular-nums text-slate-300">{l.count}</span>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                </div>

                {stats.chatCluster.uncovered.length > 0 && (
                  <div className="rounded-xl border border-amber-700/50 bg-amber-950/20 p-4">
                    <div className="mb-1 text-sm font-semibold text-amber-300">未覆盖问题（FAQ/话术补强建议）</div>
                    <div className="mb-3 text-[11px] text-amber-500/70">
                      未命中任何已知主题、且按相似问题聚合后的高频疑问 — 优先补进 FAQ 或知识库。
                    </div>
                    <div className="space-y-3">
                      {stats.chatCluster.uncovered.map((u, i) => (
                        <div key={i} className="rounded-lg bg-slate-900/60 p-2.5">
                          <div className="flex items-start gap-2 text-xs">
                            <span className="shrink-0 rounded bg-amber-900/40 px-1.5 text-[10px] text-amber-300 tabular-nums">×{u.count}</span>
                            <span className="shrink-0 rounded bg-slate-800 px-1.5 text-[10px] text-slate-400">{LANG_NAMES[u.lang] ?? u.lang}</span>
                            <span className="text-slate-200">{u.q}</span>
                          </div>
                          <div className="mt-2 flex items-end gap-2">
                            <textarea
                              value={drafts[u.q] ?? ""}
                              onChange={(e) => setDrafts((d) => ({ ...d, [u.q]: e.target.value }))}
                              placeholder="写一个标准答案，保存后 AI 立即会答（可用中文，AI 会按客户语言回复）"
                              rows={2}
                              className="flex-1 resize-y rounded-lg border border-slate-700 bg-slate-950 px-2.5 py-1.5 text-xs text-slate-200 outline-none focus:border-emerald-500"
                            />
                            <button
                              onClick={() => saveKb(u.q, u.lang)}
                              disabled={savingQ === u.q || !(drafts[u.q] ?? "").trim()}
                              className="shrink-0 rounded-lg bg-emerald-500 px-3 py-1.5 text-xs font-medium text-slate-950 disabled:opacity-40"
                            >
                              {savingQ === u.q ? "保存中…" : "补进知识库"}
                            </button>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}

            <div className="rounded-xl border border-emerald-700/40 bg-emerald-950/10 p-4">
              <div className="mb-1 flex items-center justify-between">
                <div className="text-sm font-semibold text-emerald-300">知识库补充（运营自助 · AI 实时生效）</div>
                <span className="text-[11px] text-slate-500">{kb.length} 条</span>
              </div>
              <div className="mb-3 text-[11px] text-slate-500">
                这里的问答会注入 AI 的 grounding，优先于默认话术。删除后约 10 秒内全网生效。
              </div>
              {kb.length === 0 && <div className="text-xs text-slate-500">暂无补充。可在上方「未覆盖问题」直接补答案。</div>}
              <div className="space-y-2">
                {kb.map((e) => (
                  <div key={e.id} className="rounded-lg bg-slate-900/60 p-2.5 text-xs">
                    <div className="flex items-start justify-between gap-2">
                      <div className="font-medium text-slate-200">Q: {e.q}</div>
                      <button
                        onClick={() => delKb(e.id)}
                        className="shrink-0 rounded bg-slate-800 px-2 py-0.5 text-[10px] text-rose-300 hover:bg-rose-900/40"
                      >
                        删除
                      </button>
                    </div>
                    <div className="mt-1 whitespace-pre-wrap text-slate-400">A: {e.a}</div>
                  </div>
                ))}
              </div>
            </div>
              </>
            )}

            {tab === "crm" && (
            <div className="rounded-xl border border-violet-700/40 bg-violet-950/10 p-4">
              <div className="mb-1 text-sm font-semibold text-violet-300">折扣码核销（客服用）</div>
              <div className="mb-3 text-[11px] text-slate-500">
                输入客户出示的专属码（如 HL-XXXX），校验是否有效并标记已核销。
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <input
                  value={redeemCode}
                  onChange={(e) => setRedeemCode(e.target.value.toUpperCase())}
                  placeholder="HL-XXXX"
                  className="w-44 rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 font-mono text-sm tracking-widest text-slate-200 outline-none focus:border-violet-500"
                  onKeyDown={(e) => e.key === "Enter" && redeem()}
                />
                <button
                  onClick={redeem}
                  disabled={redeeming || !redeemCode.trim()}
                  className="rounded-lg bg-violet-500 px-4 py-1.5 text-xs font-medium text-slate-950 disabled:opacity-40"
                >
                  {redeeming ? "校验中…" : "核销"}
                </button>
                {redeemMsg && <span className="text-xs text-slate-300">{redeemMsg}</span>}
              </div>

              <div className="mt-4">
                {/* 核销率统计（总体 + 按语言） */}
                {unlockStats && unlockStats.overall.issued > 0 && (
                  <div className="mb-3 rounded-lg border border-slate-800 bg-slate-950/40 p-3">
                    <div className="mb-2 flex items-center justify-between">
                      <span className="text-[11px] font-semibold text-slate-400">核销率</span>
                      <span className="text-[11px] text-slate-300">
                        总体 <span className="font-semibold text-violet-300">{Math.round(unlockStats.overall.rate * 100)}%</span>
                        <span className="ml-1 text-slate-600">（{unlockStats.overall.redeemed}/{unlockStats.overall.issued}）</span>
                      </span>
                    </div>
                    <div className="space-y-1.5">
                      {unlockStats.byLang.map((g) => (
                        <div key={g.key} className="flex items-center gap-2 text-[11px]" title={`${g.redeemed}/${g.issued}`}>
                          <span className="w-12 shrink-0 truncate text-slate-500">{g.key}</span>
                          <div className="h-3 flex-1 overflow-hidden rounded bg-slate-800">
                            <div className="h-full rounded bg-violet-500" style={{ width: `${Math.round(g.rate * 100)}%` }} />
                          </div>
                          <span className="w-16 shrink-0 text-right tabular-nums text-slate-400">
                            {Math.round(g.rate * 100)}% <span className="text-slate-600">{g.redeemed}/{g.issued}</span>
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* 批量运营工具栏 */}
                <div className="mb-2 flex flex-wrap items-center gap-1.5">
                  <button
                    onClick={() => extendAllPending(7)}
                    disabled={codesBusy}
                    className="rounded-md border border-amber-700/50 bg-amber-900/20 px-2.5 py-1 text-[11px] text-amber-200 hover:border-amber-500 disabled:opacity-40"
                  >
                    延长待核销 +7天
                  </button>
                  <button
                    onClick={voidExpiredCodes}
                    disabled={codesBusy}
                    className="rounded-md border border-rose-700/50 bg-rose-900/20 px-2.5 py-1 text-[11px] text-rose-200 hover:border-rose-500 disabled:opacity-40"
                  >
                    清理过期码
                  </button>
                </div>

                <div className="mb-2 flex items-center justify-between">
                  <div className="text-[11px] text-slate-400">
                    已发 {codes.length} · 已核销 {codes.filter((c) => c.redeemed).length} · 待核销{" "}
                    {codes.filter((c) => !c.redeemed && !(c.expiresAt && Date.now() > Date.parse(c.expiresAt))).length} · 已过期{" "}
                    {codes.filter((c) => !c.redeemed && !!c.expiresAt && Date.now() > Date.parse(c.expiresAt)).length}
                  </div>
                  <div className="flex gap-1">
                    {(["all", "unused", "used"] as const).map((f) => (
                      <button
                        key={f}
                        onClick={() => setCodesFilter(f)}
                        className={`rounded-md px-2 py-0.5 text-[11px] ${codesFilter === f ? "bg-violet-500 text-slate-950" : "bg-slate-800 text-slate-400"}`}
                      >
                        {f === "all" ? "全部" : f === "unused" ? "未用" : "已用"}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="max-h-72 space-y-1 overflow-y-auto">
                  {codes
                    .filter((c) => (codesFilter === "all" ? true : codesFilter === "used" ? c.redeemed : !c.redeemed))
                    .map((c) => {
                      const expired = !c.redeemed && !!c.expiresAt && Date.now() > Date.parse(c.expiresAt);
                      return (
                        <div
                          key={c.code}
                          className="flex items-center justify-between gap-2 rounded-lg border border-slate-800 bg-slate-950/50 px-3 py-1.5 text-xs"
                        >
                          <div className="min-w-0">
                            <span className={`font-mono tracking-widest ${expired ? "text-slate-500 line-through" : "text-cyan-300"}`}>{c.code}</span>
                            <span className="ml-2 text-slate-400">{c.contact ?? c.name ?? `tg:${c.userId}`}</span>
                            <span className="ml-2 text-[10px] text-slate-600">
                              {String(c.issuedAt).slice(5, 16).replace("T", " ")}
                              {c.expiresAt && <> · 至 {String(c.expiresAt).slice(5, 10)}</>}
                            </span>
                          </div>
                          {c.redeemed ? (
                            <span className="shrink-0 rounded-md bg-slate-800 px-2 py-0.5 text-[10px] text-slate-500">
                              已核销 {c.redeemedAt ? String(c.redeemedAt).slice(5, 16).replace("T", " ") : ""}
                            </span>
                          ) : expired ? (
                            <div className="flex shrink-0 items-center gap-1">
                              <span className="rounded-md bg-rose-900/40 px-2 py-0.5 text-[10px] text-rose-300">已过期</span>
                              <button
                                onClick={() => extendOne(c.code, 7)}
                                title="复活并延长 7 天"
                                className="rounded-md border border-amber-700/50 px-2 py-0.5 text-[10px] text-amber-300 hover:border-amber-500"
                              >
                                +7天
                              </button>
                            </div>
                          ) : (
                            <div className="flex shrink-0 items-center gap-1">
                              <button
                                onClick={() => extendOne(c.code, 7)}
                                title="延长有效期 7 天"
                                className="rounded-md border border-slate-700 px-2 py-0.5 text-[10px] text-slate-400 hover:border-amber-500 hover:text-amber-300"
                              >
                                +7天
                              </button>
                              <button
                                onClick={() => redeemOne(c.code)}
                                className="rounded-md bg-emerald-500 px-2.5 py-0.5 text-[10px] font-medium text-slate-950"
                              >
                                核销
                              </button>
                            </div>
                          )}
                        </div>
                      );
                    })}
                  {codes.length === 0 && <div className="py-3 text-center text-[11px] text-slate-600">暂无已发放的折扣码</div>}
                </div>
              </div>
            </div>
            )}

            {tab === "content" && (
            <div className="space-y-3">
            <div className="rounded-xl border border-cyan-700/40 bg-cyan-950/10 p-4">
              <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
                <div className="text-sm font-semibold text-cyan-300">一键发频道 / 群（Bot 代发）</div>
                <div className="flex flex-wrap items-center gap-2">
                  <select
                    value={catTarget}
                    onChange={(e) => setCatTarget(e.target.value as "channel" | "group" | "both")}
                    className="rounded-lg border border-slate-700 bg-slate-900 px-2 py-1.5 text-xs text-slate-200"
                  >
                    <option value="channel">频道</option>
                    <option value="group">群</option>
                    <option value="both">频道+群</option>
                  </select>
                  <select
                    value={catLang}
                    onChange={(e) => setCatLang(e.target.value as "zh" | "en")}
                    className="rounded-lg border border-slate-700 bg-slate-900 px-2 py-1.5 text-xs text-slate-200"
                  >
                    <option value="zh">中文</option>
                    <option value="en">English</option>
                  </select>
                  <button
                    onClick={publishCatalog}
                    disabled={genning}
                    className="rounded-lg border border-cyan-600/50 bg-cyan-900/30 px-3 py-1.5 text-xs font-medium text-cyan-200 disabled:opacity-50"
                  >
                    📦 发布产品目录
                  </button>
                  <button
                    onClick={publishDailyNow}
                    disabled={genning}
                    className="rounded-lg border border-emerald-600/50 bg-emerald-900/30 px-3 py-1.5 text-xs font-medium text-emerald-200 disabled:opacity-50"
                  >
                    🖼 发布今日(带图)
                  </button>
                  <button
                    onClick={genDaily}
                    disabled={genning}
                    className="rounded-lg bg-gradient-to-r from-fuchsia-500 to-cyan-500 px-3 py-1.5 text-xs font-medium text-slate-950 disabled:opacity-50"
                  >
                    {genning ? "处理中…" : "✨ 生成今日草稿"}
                  </button>
                </div>
              </div>
              <div className="mb-3 text-[11px] text-slate-500">
                需先把 Bot 设为频道/群管理员。支持 HTML（&lt;b&gt;粗体&lt;/b&gt;）。底部可自动附四宫格按钮。AI 选题每天可自动生成草稿。
              </div>
              <textarea
                value={bcText}
                onChange={(e) => setBcText(e.target.value)}
                placeholder="输入要发布的内容，例如：本周新增 3 个出海成交案例…"
                rows={4}
                className="w-full resize-y rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-200 outline-none focus:border-cyan-500"
              />
              <div className="mt-2 flex flex-wrap items-center gap-3">
                <select
                  value={bcTarget}
                  onChange={(e) => setBcTarget(e.target.value as BroadcastTarget)}
                  className="rounded-lg border border-slate-700 bg-slate-950 px-2.5 py-1.5 text-xs text-slate-200 outline-none"
                >
                  <option value="channel">仅频道</option>
                  <option value="group">仅交流群</option>
                  <option value="both">频道 + 群</option>
                </select>
                <label className="flex items-center gap-1.5 text-xs text-slate-400">
                  <input type="checkbox" checked={bcButton} onChange={(e) => setBcButton(e.target.checked)} />
                  附四宫格按钮
                </label>
                {templates.length > 0 && (
                  <select
                    defaultValue=""
                    onChange={(e) => { applyTemplate(e.target.value); e.target.value = ""; }}
                    className="rounded-lg border border-slate-700 bg-slate-950 px-2.5 py-1.5 text-xs text-slate-300 outline-none"
                  >
                    <option value="" disabled>载入模板…</option>
                    {templates.map((t) => (
                      <option key={t.id} value={t.id}>{t.name}</option>
                    ))}
                  </select>
                )}
                <button onClick={saveTemplate} disabled={!bcText.trim()} className="rounded-lg border border-slate-600 px-3 py-1.5 text-xs text-slate-300 disabled:opacity-40">
                  存为模板
                </button>
                <button
                  onClick={broadcast}
                  disabled={bcSending || !bcText.trim()}
                  className="rounded-lg bg-cyan-500 px-4 py-1.5 text-xs font-medium text-slate-950 disabled:opacity-40"
                >
                  {bcSending ? "发送中…" : "立即发布"}
                </button>
                {bcMsg && <span className="text-xs text-slate-300">{bcMsg}</span>}
              </div>

              <div className="mt-2 flex flex-wrap items-center gap-3 border-t border-slate-800 pt-2">
                <span className="text-[11px] text-slate-500">定时发布：</span>
                <input
                  type="datetime-local"
                  value={bcRunAt}
                  onChange={(e) => setBcRunAt(e.target.value)}
                  className="rounded-lg border border-slate-700 bg-slate-950 px-2.5 py-1.5 text-xs text-slate-200 outline-none"
                />
                <button
                  onClick={schedulePost}
                  disabled={bcSending || !bcText.trim() || !bcRunAt}
                  className="rounded-lg bg-amber-500 px-4 py-1.5 text-xs font-medium text-slate-950 disabled:opacity-40"
                >
                  加入定时队列
                </button>
              </div>

              {aiDrafts.length > 0 && (
                <div className="mt-3">
                  <div className="mb-1 text-[11px] font-semibold text-fuchsia-300">AI 草稿（{aiDrafts.length}）· 点击载入到上方编辑发布</div>
                  <div className="space-y-1.5">
                    {aiDrafts.slice(0, 6).map((d) => (
                      <div key={d.id} className="flex items-start justify-between gap-2 rounded bg-slate-900/60 p-1.5 text-[11px]">
                        <button onClick={() => applyDraft(d.id)} className="min-w-0 text-left hover:text-cyan-300" title="点击载入">
                          <span className="text-slate-500">{String(d.createdAt).slice(5, 16).replace("T", " ")} · {d.theme ?? "AI"}</span>
                          <div className="truncate text-slate-300">{d.text.replace(/<[^>]+>/g, "")}</div>
                        </button>
                        <button onClick={() => delDraft(d.id)} className="shrink-0 rounded bg-slate-800 px-1.5 text-[10px] text-rose-300">删</button>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              <div className="mt-3 space-y-3">
                {/* 内容运营日历 */}
                <div>
                  <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                    <div className="text-[11px] font-semibold text-slate-400">
                      定时发布 · 共 {scheduled.length} 条（待发 <span className="text-amber-300">{pendingCount}</span>）
                    </div>
                    <div className="flex items-center gap-1.5">
                      {calView === "week" && (
                        <>
                          <button onClick={() => setWeekStart((w) => addDaysKey(w, -7))} title="上一周" className="rounded border border-slate-700 px-1.5 py-0.5 text-xs text-slate-400 hover:border-cyan-500">‹</button>
                          <span className="tabular-nums text-[11px] text-slate-400">{weekStart.slice(5)} ~ {addDaysKey(weekStart, 6).slice(5)}</span>
                          <button onClick={() => setWeekStart((w) => addDaysKey(w, 7))} title="下一周" className="rounded border border-slate-700 px-1.5 py-0.5 text-xs text-slate-400 hover:border-cyan-500">›</button>
                          <button onClick={() => setWeekStart(weekStartKey(todayLocalKey()))} className="rounded border border-slate-700 px-2 py-0.5 text-[11px] text-slate-400 hover:border-cyan-500">本周</button>
                        </>
                      )}
                      <div className="ml-1 flex rounded-lg border border-slate-700 p-0.5">
                        {(["week", "list"] as const).map((v) => (
                          <button
                            key={v}
                            onClick={() => setCalView(v)}
                            className={`rounded px-2 py-0.5 text-[11px] ${calView === v ? "bg-cyan-500 text-slate-950" : "text-slate-400"}`}
                          >
                            {v === "week" ? "日历" : "列表"}
                          </button>
                        ))}
                      </div>
                    </div>
                  </div>

                  {calView === "week" ? (
                    <>
                      <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-4 lg:grid-cols-7">
                        {calDays.map((day, i) => {
                          const posts = scheduled
                            .filter((p) => localDayKey(p.runAt) === day)
                            .sort((a, b) => (a.runAt < b.runAt ? -1 : 1));
                          const isToday = day === calToday;
                          const isOver = dragOverDay === day;
                          return (
                            <div
                              key={day}
                              onDragOver={(e) => { e.preventDefault(); setDragOverDay(day); }}
                              onDragLeave={() => setDragOverDay((d) => (d === day ? null : d))}
                              onDrop={() => { if (dragId) void reschedulePost(dragId, day); setDragId(null); setDragOverDay(null); }}
                              className={`min-h-[88px] rounded-lg border p-1.5 transition-colors ${isOver ? "border-cyan-400 bg-cyan-950/30" : isToday ? "border-cyan-700/50 bg-slate-900/60" : "border-slate-800 bg-slate-900/40"}`}
                            >
                              <div className={`mb-1 flex items-center justify-between text-[10px] ${isToday ? "text-cyan-300" : "text-slate-500"}`}>
                                <span>{WEEKDAY_CN[i]} {day.slice(5)}</span>
                                {isToday && <span className="rounded bg-cyan-500/20 px-1">今天</span>}
                              </div>
                              <div className="space-y-1">
                                {posts.map((p) => {
                                  const stCls = p.status === "sent"
                                    ? "border border-emerald-800/40 bg-emerald-900/25"
                                    : p.status === "failed"
                                    ? "border border-rose-800/40 bg-rose-900/25"
                                    : "border border-amber-800/40 bg-amber-900/25";
                                  return (
                                    <div
                                      key={p.id}
                                      draggable={p.status === "pending"}
                                      onDragStart={() => setDragId(p.id)}
                                      onDragEnd={() => { setDragId(null); setDragOverDay(null); }}
                                      title={p.text.replace(/<[^>]+>/g, "")}
                                      className={`group rounded px-1 py-0.5 text-[10px] ${stCls} ${p.status === "pending" ? "cursor-grab active:cursor-grabbing" : "opacity-80"}`}
                                    >
                                      <div className="flex items-center justify-between gap-1">
                                        <span className="tabular-nums text-slate-400">{localHM(p.runAt)} · {TARGET_LABEL[p.target]}</span>
                                        <button onClick={() => delScheduled(p.id)} title="删除" className="text-rose-300/70 opacity-0 transition-opacity hover:text-rose-300 group-hover:opacity-100">×</button>
                                      </div>
                                      <div className="truncate text-slate-300/90">{p.text.replace(/<[^>]+>/g, "")}</div>
                                    </div>
                                  );
                                })}
                                {posts.length === 0 && <div className="py-2 text-center text-[10px] text-slate-700">空</div>}
                              </div>
                            </div>
                          );
                        })}
                      </div>
                      <div className="mt-1.5 text-[10px] text-slate-600">提示：拖动「待发」卡片到其它日期即可改期（保持原时刻）；已发 / 失败为历史，不可拖动。</div>
                    </>
                  ) : (
                    <div className="space-y-1.5">
                      {scheduled.slice(0, 20).map((p) => (
                        <div key={p.id} className="flex items-start justify-between gap-2 rounded bg-slate-900/60 p-1.5 text-[11px]">
                          <div className="min-w-0">
                            <span className={`mr-1 rounded px-1 ${p.status === "sent" ? "bg-emerald-900/40 text-emerald-300" : p.status === "failed" ? "bg-rose-900/40 text-rose-300" : "bg-amber-900/40 text-amber-300"}`}>
                              {p.status === "sent" ? "已发" : p.status === "failed" ? "失败" : "待发"}
                            </span>
                            <span className="text-slate-500">{localDayKey(p.runAt).slice(5)} {localHM(p.runAt)} · {TARGET_LABEL[p.target]}</span>
                            <div className="truncate text-slate-300" title={p.text}>{p.text.replace(/<[^>]+>/g, "")}</div>
                            {p.error && <div className="truncate text-rose-400" title={p.error}>{p.error}</div>}
                          </div>
                          <button onClick={() => delScheduled(p.id)} className="shrink-0 rounded bg-slate-800 px-1.5 text-[10px] text-rose-300">删</button>
                        </div>
                      ))}
                      {scheduled.length === 0 && <div className="text-[11px] text-slate-600">暂无</div>}
                    </div>
                  )}
                </div>

                {/* 模板 */}
                <div>
                  <div className="mb-1 text-[11px] font-semibold text-slate-400">模板（{templates.length}）· 点击载入到上方编辑</div>
                  <div className="flex flex-wrap gap-1.5">
                    {templates.map((t) => (
                      <div key={t.id} className="flex items-center gap-1.5 rounded bg-slate-900/60 px-2 py-1 text-[11px]">
                        <button onClick={() => applyTemplate(t.id)} className="min-w-0 truncate text-left text-slate-300 hover:text-cyan-300" title="点击载入">
                          {t.name} <span className="text-slate-600">· {TARGET_LABEL[t.target]}</span>
                        </button>
                        <button onClick={() => delTemplate(t.id)} className="shrink-0 text-rose-300/70 hover:text-rose-300">×</button>
                      </div>
                    ))}
                    {templates.length === 0 && <div className="text-[11px] text-slate-600">暂无模板</div>}
                  </div>
                </div>
              </div>
            </div>

            <SectionCard title="最近发布 & 影响参考（近 14 天）" Icon={TrendingUp} accent="text-amber-300">
              <p className="mb-2 text-[11px] leading-relaxed text-slate-500">
                每次发布后附上「当日 PV / 当日留资 / 次日留资」作为相关性参考——不强行宣称因果（同日多发、自然波动会误导）。趋势图上的橙色虚线即发布日。
              </p>
              {(stats.publishes?.length ?? 0) === 0 ? (
                <div className="py-4 text-center text-[11px] text-slate-600">近 14 天还没有发布记录，发一条试试 ↑</div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[640px] text-left text-[12px]">
                    <thead>
                      <tr className="border-b border-slate-800 text-[11px] text-slate-500">
                        <th className="py-1.5 pr-2 font-medium">时间</th>
                        <th className="py-1.5 pr-2 font-medium">类型</th>
                        <th className="py-1.5 pr-2 font-medium">目标</th>
                        <th className="py-1.5 pr-2 font-medium">内容</th>
                        <th className="py-1.5 pr-2 text-right font-medium">当日PV</th>
                        <th className="py-1.5 pr-2 text-right font-medium">当日留资</th>
                        <th className="py-1.5 text-right font-medium">次日留资</th>
                      </tr>
                    </thead>
                    <tbody>
                      {stats.publishes!.slice(0, 30).map((p, i) => (
                        <tr key={`${p.t}-${i}`} className="border-b border-slate-900/60">
                          <td className="whitespace-nowrap py-1.5 pr-2 text-slate-400">{p.t.slice(5, 16).replace("T", " ")}</td>
                          <td className="py-1.5 pr-2">
                            <span className={`rounded px-1.5 py-0.5 text-[10px] ${PUBLISH_KIND[p.kind]?.cls ?? "bg-slate-800 text-slate-300"}`}>
                              {PUBLISH_KIND[p.kind]?.label ?? p.kind}
                            </span>
                          </td>
                          <td className="whitespace-nowrap py-1.5 pr-2 text-slate-400">{TARGET_LABEL[p.target as BroadcastTarget] ?? p.target}</td>
                          <td className="max-w-[220px] truncate py-1.5 pr-2 text-slate-300" title={p.summary}>{p.summary || "—"}</td>
                          <td className="py-1.5 pr-2 text-right text-cyan-300">{p.ref.pvSame ?? "—"}</td>
                          <td className="py-1.5 pr-2 text-right text-emerald-300">{p.ref.leadSame ?? "—"}</td>
                          <td className="py-1.5 text-right text-emerald-300/70">{p.ref.leadNext ?? "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </SectionCard>
            </div>
            )}

            {tab === "chat" && (
            <div className="rounded-xl border border-slate-700 bg-slate-900 p-4">
              <div className="mb-3 text-sm font-semibold text-slate-200">客户最近在问（AI 对话，最多 20 条）</div>
              <div className="space-y-1.5">
                {(stats.recentQuestions ?? []).map((q, i) => (
                  <div key={i} className="flex items-start gap-2 text-xs">
                    <span className="shrink-0 text-slate-500">{String(q.t).slice(5, 16).replace("T", " ")}</span>
                    <span className="shrink-0 rounded bg-slate-800 px-1.5 text-[10px] text-slate-400">{q.source}</span>
                    <span className="text-slate-200">{q.q}</span>
                  </div>
                ))}
                {(stats.recentQuestions ?? []).length === 0 && (
                  <div className="text-xs text-slate-500">暂无对话</div>
                )}
              </div>
            </div>
            )}

            {tab === "crm" && (
            <div className="rounded-xl border border-slate-700 bg-slate-900 p-4">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                <div className="text-sm font-semibold text-slate-200">留资 CRM（去重 · 状态机）</div>
                <div className="flex flex-wrap gap-1.5">
                  {(["all", ...STATUS_ORDER] as const).map((s) => {
                    const n = crmData?.counts?.[s] ?? 0;
                    const active = leadFilter === s;
                    return (
                      <button
                        key={s}
                        onClick={() => { setLeadFilter(s); setCrmPage(1); }}
                        className={`rounded-full px-2.5 py-0.5 text-[11px] ${active ? "bg-cyan-500 text-slate-950" : "bg-slate-800 text-slate-400"}`}
                      >
                        {s === "all" ? "全部" : STATUS_META[s].label} {n}
                      </button>
                    );
                  })}
                </div>
              </div>
              <div className="mb-3 flex flex-wrap items-center gap-2">
                <div className="relative flex-1 min-w-[180px]">
                  <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-500" />
                  <input
                    value={crmSearch}
                    onChange={(e) => { setCrmSearch(e.target.value); setCrmPage(1); }}
                    placeholder="搜索称呼 / 联系方式 / 意向 / 来源"
                    className="w-full rounded-lg border border-slate-700 bg-slate-950 py-1.5 pl-8 pr-3 text-xs text-slate-200 outline-none focus:border-cyan-500"
                  />
                </div>
                <div className="flex items-center gap-1 text-[11px] text-slate-500">
                  <input
                    type="date"
                    value={crmFrom}
                    onChange={(e) => { setCrmFrom(e.target.value); setCrmPage(1); }}
                    className="rounded-lg border border-slate-700 bg-slate-950 px-2 py-1.5 text-xs text-slate-300 outline-none focus:border-cyan-500"
                  />
                  <span>~</span>
                  <input
                    type="date"
                    value={crmTo}
                    onChange={(e) => { setCrmTo(e.target.value); setCrmPage(1); }}
                    className="rounded-lg border border-slate-700 bg-slate-950 px-2 py-1.5 text-xs text-slate-300 outline-none focus:border-cyan-500"
                  />
                  {(crmFrom || crmTo) && (
                    <button
                      onClick={() => { setCrmFrom(""); setCrmTo(""); setCrmPage(1); }}
                      className="text-slate-500 hover:text-slate-300"
                      title="清除日期"
                    >
                      <XCircle className="h-3.5 w-3.5" />
                    </button>
                  )}
                </div>
                <span className="text-[11px] text-slate-500">{crmData?.total ?? 0} 条</span>
                <button
                  onClick={() => exportLeadsCsv()}
                  disabled={(crmData?.total ?? 0) === 0}
                  className="flex items-center gap-1 rounded-lg border border-slate-700 px-2.5 py-1.5 text-xs text-slate-300 hover:border-emerald-500 disabled:opacity-40"
                >
                  <Download className="h-3.5 w-3.5" />
                  导出 CSV
                </button>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead className="text-slate-500">
                    <tr>
                      {([
                        ["lastSeen", "时间"],
                        ["name", "称呼"],
                      ] as const).map(([field, label]) => (
                        <th key={field} className="py-1 pr-3">
                          <button
                            onClick={() => toggleSort(field)}
                            className={`inline-flex items-center gap-0.5 hover:text-slate-300 ${crmSort === field ? "text-cyan-400" : ""}`}
                          >
                            {label}
                            {crmSort === field && (crmDir === "desc" ? <ArrowDownRight className="h-3 w-3" /> : <ArrowUpRight className="h-3 w-3" />)}
                          </button>
                        </th>
                      ))}
                      <th className="py-1 pr-3">联系方式</th>
                      <th className="py-1 pr-3">意向</th>
                      <th className="py-1 pr-3">来源</th>
                      <th className="py-1 pr-3">
                        <button
                          onClick={() => toggleSort("status")}
                          className={`inline-flex items-center gap-0.5 hover:text-slate-300 ${crmSort === "status" ? "text-cyan-400" : ""}`}
                        >
                          状态 / 操作
                          {crmSort === "status" && (crmDir === "desc" ? <ArrowDownRight className="h-3 w-3" /> : <ArrowUpRight className="h-3 w-3" />)}
                        </button>
                      </th>
                    </tr>
                  </thead>
                  <tbody className="text-slate-300">
                    {crmRows.map((l) => {
                      const tg = tgLink(l.contact);
                      return (
                        <tr key={l.id} className="border-t border-slate-800">
                          <td className="py-1.5 pr-3 whitespace-nowrap text-slate-500">
                            {String(l.t).slice(5, 16).replace("T", " ")}
                          </td>
                          <td className="py-1.5 pr-3">{l.name || "-"}{l.count > 1 && <span className="ml-1 text-[10px] text-slate-500">×{l.count}</span>}</td>
                          <td className="py-1.5 pr-3">
                            <div className="flex items-center gap-1.5">
                              <span className="font-mono text-cyan-400">{l.contact}</span>
                              <button
                                onClick={() => copyText(l.contact)}
                                title="复制联系方式"
                                className="shrink-0 text-slate-500 hover:text-cyan-300"
                              >
                                <Copy className="h-3 w-3" />
                              </button>
                              {tg && (
                                <a
                                  href={tg}
                                  target="_blank"
                                  rel="noreferrer"
                                  title="在 Telegram 打开"
                                  className="shrink-0 text-slate-500 hover:text-sky-300"
                                >
                                  <ExternalLink className="h-3 w-3" />
                                </a>
                              )}
                            </div>
                          </td>
                          <td className="py-1.5 pr-3">{l.interest || "-"}</td>
                          <td className="py-1.5 pr-3">{l.source}</td>
                          <td className="py-1.5 pr-3">
                            <div className="flex items-center gap-1">
                              <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] ${STATUS_META[l.status].cls}`}>
                                {STATUS_META[l.status].label}
                              </span>
                              <select
                                value={l.status}
                                onChange={(e) => changeLeadStatus(l.id, e.target.value as LeadStatus)}
                                className="rounded border border-slate-700 bg-slate-950 px-1 py-0.5 text-[10px] text-slate-300 outline-none"
                              >
                                {STATUS_ORDER.map((s) => (
                                  <option key={s} value={s}>{STATUS_META[s].label}</option>
                                ))}
                              </select>
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                    {crmRows.length === 0 && (
                      <tr>
                        <td colSpan={6} className="py-3 text-center text-slate-500">
                          {crmLoading || !crmData ? "加载中…" : (crmData.counts?.all ?? 0) === 0 ? "暂无留资" : "无匹配结果"}
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>

              {/* pagination */}
              {crmData && crmData.pages > 1 && (
                <div className="mt-3 flex items-center justify-center gap-3 text-xs text-slate-400">
                  <button
                    onClick={() => setCrmPage((p) => Math.max(1, p - 1))}
                    disabled={crmData.page <= 1}
                    className="rounded-lg border border-slate-700 px-3 py-1 hover:border-cyan-500 disabled:opacity-40"
                  >
                    上一页
                  </button>
                  <span className="tabular-nums">
                    第 {crmData.page} / {crmData.pages} 页 · 共 {crmData.total} 条
                  </span>
                  <button
                    onClick={() => setCrmPage((p) => Math.min(crmData.pages, p + 1))}
                    disabled={crmData.page >= crmData.pages}
                    className="rounded-lg border border-slate-700 px-3 py-1 hover:border-cyan-500 disabled:opacity-40"
                  >
                    下一页
                  </button>
                </div>
              )}
            </div>
            )}
          </main>
        </>
      )}
    </div>
  );
}
