import { NextRequest, NextResponse } from "next/server";
import { listLeads, type LeadEntry, type LeadStatus } from "@/lib/lead-store";
import { requireAdmin } from "@/lib/admin-auth";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const TZ_OFFSET_H = Number(process.env.TZ_OFFSET ?? 8);
const TZ_MS = TZ_OFFSET_H * 3600 * 1000;

function dayKey(iso: unknown): string {
  const t = Date.parse(String(iso ?? ""));
  if (isNaN(t)) return "";
  return new Date(t + TZ_MS).toISOString().slice(0, 10);
}

const STATUSES: LeadStatus[] = ["new", "contacted", "won", "lost"];
const STATUS_RANK: Record<LeadStatus, number> = { new: 0, contacted: 1, won: 2, lost: 3 };
type SortField = "lastSeen" | "firstSeen" | "count" | "name" | "status";
const SORT_FIELDS: SortField[] = ["lastSeen", "firstSeen", "count", "name", "status"];

export async function GET(req: NextRequest) {
  if (!process.env.TELEGRAM_SETUP_KEY && !process.env.ADMIN_KEY) {
    return NextResponse.json({ ok: false, error: "not_configured" }, { status: 503 });
  }
  if (!requireAdmin(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }

  const sp = req.nextUrl.searchParams;
  const status = sp.get("status") as LeadStatus | "all" | null;
  const q = (sp.get("q") || "").trim().toLowerCase();
  const from = sp.get("from") || ""; // YYYY-MM-DD inclusive
  const to = sp.get("to") || ""; // YYYY-MM-DD inclusive
  const sort = (SORT_FIELDS.includes(sp.get("sort") as SortField) ? sp.get("sort") : "lastSeen") as SortField;
  const dir = sp.get("dir") === "asc" ? "asc" : "desc";
  const all = sp.get("all") === "1"; // export mode: skip pagination
  const page = Math.max(1, Number(sp.get("page") || 1));
  const pageSize = Math.min(200, Math.max(5, Number(sp.get("pageSize") || 20)));

  const leads = await listLeads();

  // base filter: search + date range (status applied after, so pills can show per-status counts of this base)
  const base = leads.filter((l) => {
    if (q) {
      const hay = `${l.name ?? ""} ${l.contact ?? ""} ${l.interest ?? ""} ${l.source ?? ""} ${l.lang ?? ""}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    if (from || to) {
      const d = dayKey(l.lastSeen);
      if (!d) return false;
      if (from && d < from) return false;
      if (to && d > to) return false;
    }
    return true;
  });

  const counts: Record<string, number> = { all: base.length, new: 0, contacted: 0, won: 0, lost: 0 };
  for (const l of base) counts[l.status] = (counts[l.status] ?? 0) + 1;

  const filtered = status && status !== "all" ? base.filter((l) => l.status === status) : base;

  const sign = dir === "asc" ? 1 : -1;
  const sorted = filtered.slice().sort((a, b) => {
    let cmp = 0;
    switch (sort) {
      case "count":
        cmp = a.count - b.count;
        break;
      case "name":
        cmp = (a.name || "").localeCompare(b.name || "");
        break;
      case "status":
        cmp = STATUS_RANK[a.status] - STATUS_RANK[b.status];
        break;
      case "firstSeen":
        cmp = (a.firstSeen || "").localeCompare(b.firstSeen || "");
        break;
      default:
        cmp = (a.lastSeen || "").localeCompare(b.lastSeen || "");
    }
    if (cmp === 0) cmp = (a.lastSeen || "").localeCompare(b.lastSeen || ""); // stable tiebreak
    return cmp * sign;
  });

  const total = sorted.length;
  const pages = Math.max(1, Math.ceil(total / pageSize));
  const slice = all ? sorted : sorted.slice((page - 1) * pageSize, (page - 1) * pageSize + pageSize);

  const rows = slice.map((l: LeadEntry) => ({
    id: l.id,
    t: l.lastSeen,
    firstSeen: l.firstSeen,
    name: l.name,
    contact: l.contact,
    interest: l.interest,
    source: l.source ?? "web",
    lang: l.lang,
    status: l.status,
    count: l.count,
    verified: l.verified ?? "",
  }));

  return NextResponse.json({
    ok: true,
    total,
    page: all ? 1 : page,
    pageSize: all ? total : pageSize,
    pages: all ? 1 : pages,
    counts,
    statuses: STATUSES,
    rows,
  });
}
