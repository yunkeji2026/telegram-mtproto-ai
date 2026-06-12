import crypto from "crypto";

/**
 * Verify Telegram Mini App initData per:
 * https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
 * Returns the parsed user id if valid (within maxAgeSec), else null.
 */
export function verifyInitData(
  initData: string,
  botToken: string,
  maxAgeSec = 86400
): { ok: boolean; userId?: number } {
  if (!initData || !botToken) return { ok: false };
  try {
    const params = new URLSearchParams(initData);
    const hash = params.get("hash");
    if (!hash) return { ok: false };

    const authDate = Number(params.get("auth_date") ?? 0);
    if (authDate && Date.now() / 1000 - authDate > maxAgeSec) {
      return { ok: false };
    }

    params.delete("hash");
    const dataCheckString = [...params.entries()]
      .map(([k, v]) => `${k}=${v}`)
      .sort()
      .join("\n");

    const secretKey = crypto
      .createHmac("sha256", "WebAppData")
      .update(botToken)
      .digest();
    const computed = crypto
      .createHmac("sha256", secretKey)
      .update(dataCheckString)
      .digest("hex");

    if (computed !== hash) return { ok: false };

    let userId: number | undefined;
    try {
      const u = JSON.parse(params.get("user") ?? "{}");
      if (u?.id) userId = Number(u.id);
    } catch {
      /* ignore */
    }
    return { ok: true, userId };
  } catch {
    return { ok: false };
  }
}
