import { NextRequest, NextResponse } from "next/server";
import { streamDeepSeek, deepseekEnabled, type ChatTurn } from "@/lib/deepseek";
import { matchFreeText, buildFallback, detectKnowledgeLang, type BotLang } from "@/lib/bot-knowledge";
import { cleanMarkdown } from "@/lib/clean-markdown";
import { logChat, dailyGuard } from "@/lib/chat-log";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// naive in-memory per-IP limiter (resets on redeploy)
const hits = new Map<string, { n: number; ts: number }>();
const WINDOW_MS = 60_000;
const MAX_PER_WINDOW = 15;

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

function textStream(text: string): ReadableStream<Uint8Array> {
  const enc = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      controller.enqueue(enc.encode(text));
      controller.close();
    },
  });
}

export async function POST(req: NextRequest) {
  const ip =
    req.headers.get("x-forwarded-for")?.split(",")[0]?.trim() ||
    req.headers.get("x-real-ip") ||
    "anon";
  if (limited(ip)) {
    return NextResponse.json({ ok: false, error: "rate_limited" }, { status: 429 });
  }

  let body: { message?: string; lang?: string; history?: ChatTurn[] };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ ok: false, error: "bad_request" }, { status: 400 });
  }

  const message = String(body?.message ?? "").trim().slice(0, 1000);
  if (!message) {
    return NextResponse.json({ ok: false, error: "empty" }, { status: 400 });
  }
  // grounding language follows the message text so non-CJK users get en facts
  // (the system prompt mirrors the user's actual language for output)
  const lang: BotLang = detectKnowledgeLang(message);
  const history: ChatTurn[] = Array.isArray(body?.history)
    ? body!.history!
        .filter((h) => (h.role === "user" || h.role === "assistant") && typeof h.content === "string")
        .map((h) => ({ role: h.role, content: String(h.content).slice(0, 800) }))
        .slice(-6)
    : [];

  const fallbackText = () => {
    const fb = matchFreeText(message, lang) ?? buildFallback(lang);
    return cleanMarkdown(fb.replace(/<\/?[^>]+>/g, ""));
  };

  // cost guard + AI availability → stream; else single-shot fallback
  const guard = dailyGuard();
  if (!deepseekEnabled() || !guard.allowed) {
    const text = fallbackText();
    void logChat({ q: message, a: text, lang, source: guard.allowed ? "kb" : "capped", ip });
    return new Response(textStream(text), {
      headers: { "Content-Type": "text/plain; charset=utf-8", "X-Chat-Source": "kb" },
    });
  }

  const upstream = await streamDeepSeek(message, lang, history);
  if (!upstream || !upstream.body) {
    const text = fallbackText();
    void logChat({ q: message, a: text, lang, source: "kb", ip });
    return new Response(textStream(text), {
      headers: { "Content-Type": "text/plain; charset=utf-8", "X-Chat-Source": "kb" },
    });
  }

  const decoder = new TextDecoder();
  const encoder = new TextEncoder();
  let buffer = "";
  let full = "";

  const stream = new ReadableStream<Uint8Array>({
    async start(controller) {
      const reader = upstream.body!.getReader();
      try {
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() ?? "";
          for (const line of lines) {
            const t = line.trim();
            if (!t.startsWith("data:")) continue;
            const payload = t.slice(5).trim();
            if (payload === "[DONE]") continue;
            try {
              const json = JSON.parse(payload);
              const delta = json?.choices?.[0]?.delta?.content;
              if (typeof delta === "string" && delta) {
                full += delta;
                controller.enqueue(encoder.encode(delta));
              }
            } catch {
              /* ignore partial json */
            }
          }
        }
      } catch {
        /* upstream aborted */
      } finally {
        controller.close();
        const clean = cleanMarkdown(full) || fallbackText();
        void logChat({ q: message, a: clean, lang, source: "ai", ip });
      }
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/plain; charset=utf-8",
      "Cache-Control": "no-cache, no-transform",
      "X-Chat-Source": "ai",
    },
  });
}
