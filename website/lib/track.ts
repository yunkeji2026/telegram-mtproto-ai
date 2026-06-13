// 会话级 id：把同一次浏览的离散事件串起来，让后端能算「真实会话漏斗转化率」
// （进入 N 个会话→最终几个留资），而非分子分母口径不一致的跨事件累加计数。
// sessionStorage 在标签页关闭即失效，天然贴合「一次访问=一个会话」；不可用时退化为内存 id。
let cachedSid: string | null = null;
function sessionId(): string {
  if (cachedSid) return cachedSid;
  const gen = () =>
    (typeof crypto !== "undefined" && crypto.randomUUID
      ? crypto.randomUUID()
      : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`);
  try {
    const k = "ml_sid";
    let s = sessionStorage.getItem(k);
    if (!s) {
      s = gen();
      sessionStorage.setItem(k, s);
    }
    cachedSid = s;
  } catch {
    cachedSid = `m-${gen()}`;
  }
  return cachedSid;
}

export function track(event: string, props?: Record<string, unknown>) {
  if (typeof window === "undefined") return;
  try {
    const body = JSON.stringify({
      event,
      props: props ?? null,
      sid: sessionId(),
      path: window.location.pathname,
      ref: document.referrer || "",
      ts: Date.now(),
    });
    if (navigator.sendBeacon) {
      navigator.sendBeacon("/api/track", new Blob([body], { type: "application/json" }));
    } else {
      fetch("/api/track", {
        method: "POST",
        body,
        headers: { "Content-Type": "application/json" },
        keepalive: true,
      }).catch(() => {});
    }
  } catch {
    /* ignore */
  }
}
