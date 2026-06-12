import { BotLang, systemPrompt } from "./bot-knowledge";
import { cleanMarkdown } from "./clean-markdown";
import { getKbExtraContext } from "./kb-extra";
import { canProceed, recordSuccess, recordFailure } from "./circuit-breaker";

const ENDPOINT = process.env.DEEPSEEK_BASE_URL || "https://api.deepseek.com/chat/completions";
const MODEL = process.env.DEEPSEEK_MODEL || "deepseek-chat";

export type ChatTurn = { role: "user" | "assistant"; content: string };

export function deepseekEnabled() {
  return Boolean(process.env.DEEPSEEK_API_KEY);
}

async function buildMessages(question: string, lang: BotLang, history: ChatTurn[]) {
  const extra = await getKbExtraContext();
  const system = extra ? `${systemPrompt(lang)}\n\n${extra}` : systemPrompt(lang);
  return [
    { role: "system", content: system },
    ...history.slice(-6),
    { role: "user", content: question.trim().slice(0, 1000) },
  ];
}

/** Generic one-shot generation with a custom system prompt (for content/copywriting). */
export async function generateText(
  system: string,
  user: string,
  timeoutMs = 22000
): Promise<string | null> {
  const key = process.env.DEEPSEEK_API_KEY;
  if (!key || !user.trim()) return null;
  if (!canProceed()) return null;
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), timeoutMs);
  try {
    const res = await fetch(ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${key}` },
      body: JSON.stringify({
        model: MODEL,
        messages: [
          { role: "system", content: system },
          { role: "user", content: user },
        ],
        temperature: 0.85,
        max_tokens: 700,
        stream: false,
      }),
      signal: ac.signal,
    });
    if (!res.ok) {
      recordFailure(`http_${res.status}`);
      return null;
    }
    const data = await res.json();
    const text = data?.choices?.[0]?.message?.content;
    if (typeof text !== "string" || !text.trim()) {
      recordFailure("empty");
      return null;
    }
    recordSuccess();
    return cleanMarkdown(text.trim());
  } catch (e) {
    recordFailure(e);
    return null;
  } finally {
    clearTimeout(timer);
  }
}

/** Streaming variant: returns the raw fetch Response (SSE body) or null. */
export async function streamDeepSeek(
  question: string,
  lang: BotLang,
  history: ChatTurn[] = [],
  timeoutMs = 20000
): Promise<Response | null> {
  const key = process.env.DEEPSEEK_API_KEY;
  if (!key || !question.trim()) return null;
  if (!canProceed()) return null;
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), timeoutMs);
  try {
    const res = await fetch(ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${key}` },
      body: JSON.stringify({
        model: MODEL,
        messages: await buildMessages(question, lang, history),
        temperature: 0.5,
        max_tokens: 600,
        stream: true,
      }),
      signal: ac.signal,
    });
    if (!res.ok || !res.body) {
      recordFailure(res.ok ? "no_body" : `http_${res.status}`);
      clearTimeout(timer);
      return null;
    }
    // streaming started successfully → close the circuit
    recordSuccess();
    res.body && clearTimeout(timer);
    return res;
  } catch (e) {
    recordFailure(e);
    clearTimeout(timer);
    return null;
  }
}

/**
 * Ask DeepSeek with grounded knowledge context.
 * Returns the answer string, or null on any failure (caller falls back).
 */
export async function askDeepSeek(
  question: string,
  lang: BotLang,
  history: ChatTurn[] = [],
  timeoutMs = 12000
): Promise<string | null> {
  const key = process.env.DEEPSEEK_API_KEY;
  if (!key) return null;
  const q = question.trim().slice(0, 1000);
  if (!q) return null;
  if (!canProceed()) return null;

  const messages = await buildMessages(q, lang, history);

  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), timeoutMs);
  try {
    const res = await fetch(ENDPOINT, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${key}`,
      },
      body: JSON.stringify({
        model: MODEL,
        messages,
        temperature: 0.5,
        max_tokens: 500,
        stream: false,
      }),
      signal: ac.signal,
    });
    if (!res.ok) {
      recordFailure(`http_${res.status}`);
      return null;
    }
    const data = await res.json();
    const text = data?.choices?.[0]?.message?.content;
    if (typeof text !== "string" || !text.trim()) {
      recordFailure("empty");
      return null;
    }
    recordSuccess();
    return cleanMarkdown(text.trim());
  } catch (e) {
    recordFailure(e);
    return null;
  } finally {
    clearTimeout(timer);
  }
}
