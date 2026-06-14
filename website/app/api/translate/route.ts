import { NextRequest, NextResponse } from "next/server";
import { deepseekEnabled } from "@/lib/deepseek";
import { dailyGuard } from "@/lib/chat-log";
import { canProceed, recordSuccess, recordFailure } from "@/lib/circuit-breaker";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const ENDPOINT = process.env.DEEPSEEK_BASE_URL || "https://api.deepseek.com/chat/completions";
const MODEL = process.env.DEEPSEEK_MODEL || "deepseek-chat";

// 通译 LingoX demo 的目标语言（展示「一句进、多语出」的实时翻译能力）。
const TARGETS = [
  { code: "en", name: "English", native: "English", flag: "🇬🇧" },
  { code: "es", name: "Spanish", native: "Español", flag: "🇪🇸" },
  { code: "pt", name: "Portuguese", native: "Português", flag: "🇧🇷" },
  { code: "ar", name: "Arabic", native: "العربية", flag: "🇸🇦" },
  { code: "ru", name: "Russian", native: "Русский", flag: "🇷🇺" },
  { code: "ja", name: "Japanese", native: "日本語", flag: "🇯🇵" },
] as const;

// 朴素 per-IP 限流（重部署即重置）：翻译比聊天更易被刷，给更紧的额度。
const hits = new Map<string, { n: number; ts: number }>();
const WINDOW_MS = 60_000;
const MAX_PER_WINDOW = 8;
function limited(ip: string) {
  const now = Date.now();
  const cur = hits.get(ip);
  if (!cur || now - cur.ts > WINDOW_MS) {
    hits.set(ip, { n: 1, ts: now });
    return false;
  }
  cur.n += 1;
  return cur.n > MAX_PER_WINDOW;
}

export async function POST(req: NextRequest) {
  const ip =
    req.headers.get("x-forwarded-for")?.split(",")[0]?.trim() ||
    req.headers.get("x-real-ip") ||
    "anon";
  if (limited(ip)) {
    return NextResponse.json({ ok: false, error: "rate_limited" }, { status: 429 });
  }

  let body: { text?: string };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ ok: false, error: "bad_request" }, { status: 400 });
  }
  const text = String(body?.text ?? "").trim().slice(0, 300);
  if (!text) {
    return NextResponse.json({ ok: false, error: "empty" }, { status: 400 });
  }

  const key = process.env.DEEPSEEK_API_KEY;
  const guard = dailyGuard();
  if (!deepseekEnabled() || !key || !guard.allowed || !canProceed()) {
    return NextResponse.json(
      { ok: false, error: guard.allowed ? "unavailable" : "capped" },
      { status: 503 }
    );
  }

  const system =
    `You are a professional translator. Translate the user's message into each of these languages: ` +
    `${TARGETS.map((t) => `${t.code} (${t.name})`).join(", ")}. ` +
    `Return ONLY a strict, minified JSON object mapping each language code to its translation string ` +
    `(e.g. {"en":"...","es":"..."}). No markdown, no comments, no extra keys. Keep translations natural and idiomatic.`;

  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), 22000);
  try {
    const res = await fetch(ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${key}` },
      body: JSON.stringify({
        model: MODEL,
        messages: [
          { role: "system", content: system },
          { role: "user", content: text },
        ],
        temperature: 0.3,
        max_tokens: 900,
        stream: false,
      }),
      signal: ac.signal,
    });
    if (!res.ok) {
      recordFailure(`http_${res.status}`);
      return NextResponse.json({ ok: false, error: "upstream" }, { status: 502 });
    }
    const data = await res.json();
    const content: string = data?.choices?.[0]?.message?.content ?? "";
    // 模型偶尔会包裹多余文字/代码块，截取第一个 {...} 块再解析
    const m = content.match(/\{[\s\S]*\}/);
    let map: Record<string, string> = {};
    try {
      map = m ? JSON.parse(m[0]) : {};
    } catch {
      recordFailure("parse");
      return NextResponse.json({ ok: false, error: "parse" }, { status: 502 });
    }
    const translations = TARGETS.map((t) => ({
      code: t.code,
      native: t.native,
      flag: t.flag,
      text: typeof map[t.code] === "string" ? map[t.code].trim() : "",
    })).filter((t) => t.text);

    if (translations.length === 0) {
      recordFailure("empty");
      return NextResponse.json({ ok: false, error: "empty" }, { status: 502 });
    }
    recordSuccess();
    return NextResponse.json({ ok: true, translations });
  } catch (e) {
    recordFailure(e);
    return NextResponse.json({ ok: false, error: "aborted" }, { status: 504 });
  } finally {
    clearTimeout(timer);
  }
}
