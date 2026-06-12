// Lightweight in-process circuit breaker for the DeepSeek dependency.
// When the upstream fails repeatedly, the circuit opens and calls short-circuit
// immediately (no waiting on timeouts), so the bot/web chat degrades to the
// grounded FAQ fallback instantly instead of hanging per request.

const FAIL_THRESHOLD = Number(process.env.DS_FAIL_THRESHOLD || 3);
const OPEN_MS = Number(process.env.DS_OPEN_MS || 60_000);

type State = "closed" | "open" | "half";

let consecutiveFailures = 0;
let openedAt = 0;
let lastError = "";
let totalTrips = 0;

export function breakerState(): {
  state: State;
  consecutiveFailures: number;
  openForMs: number;
  totalTrips: number;
  lastError: string;
} {
  const now = Date.now();
  let state: State = "closed";
  if (openedAt) {
    state = now - openedAt < OPEN_MS ? "open" : "half";
  }
  return {
    state,
    consecutiveFailures,
    openForMs: openedAt ? Math.max(0, OPEN_MS - (now - openedAt)) : 0,
    totalTrips,
    lastError,
  };
}

/** Returns true if a call may proceed (closed or half-open probe). */
export function canProceed(): boolean {
  if (!openedAt) return true;
  return Date.now() - openedAt >= OPEN_MS; // half-open: allow a probe
}

export function recordSuccess(): void {
  consecutiveFailures = 0;
  openedAt = 0;
  lastError = "";
}

export function recordFailure(err?: unknown): void {
  consecutiveFailures += 1;
  lastError = err ? String(err).slice(0, 200) : "failure";
  if (consecutiveFailures >= FAIL_THRESHOLD && !openedAt) {
    openedAt = Date.now();
    totalTrips += 1;
  } else if (openedAt) {
    // failed probe in half-open → re-open the window
    openedAt = Date.now();
  }
}
