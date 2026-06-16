import { NextRequest, NextResponse } from "next/server";
import { setupBot } from "@/lib/telegram-bot";
import { setupChannels } from "@/lib/tg-broadcast";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** One-time / maintenance: register webhook + commands + bot identity, and optionally
 *  set channel/group name+description (+ pinned overview). Protect with SETUP_KEY env.
 *
 *  Body (JSON, optional):
 *    { "channels": true|false, "setPhoto": true|false, "pinOverview": true|false, "webhook": true|false }
 *  - channels   : also apply channel/group display name + description (default: true)
 *  - setPhoto   : set channel/group avatar to the brand mark (default: true)
 *  - pinOverview: (re)post + pin the product overview to the channel (default: true).
 *                 Idempotent: if the channel's current pinned message was posted by this bot,
 *                 it is skipped to avoid duplicate posts on re-runs.
 *  - forcePin   : ignore the idempotency check and always (re)post + pin (default: false)
 *  - webhook    : (re)register the webhook (default: true). Set false to refresh only the
 *                 brand identity (name/desc/commands/menu) without re-pointing the webhook —
 *                 use this when running setup from a non-production env to avoid a webhook
 *                 secret mismatch that would 403 (mute) the live bot.
 */
export async function POST(req: NextRequest) {
  const key = process.env.TELEGRAM_SETUP_KEY;
  if (!key) {
    return NextResponse.json({ ok: false, error: "SETUP_KEY not configured" }, { status: 503 });
  }
  const hdr = req.headers.get("x-setup-key");
  if (hdr !== key) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }

  let body: {
    channels?: boolean;
    setPhoto?: boolean;
    pinOverview?: boolean;
    forcePin?: boolean;
    webhook?: boolean;
  } = {};
  try {
    body = await req.json();
  } catch {
    /* empty body ok */
  }

  const bot = await setupBot({ skipWebhook: body.webhook === false });
  const channels =
    body.channels === false
      ? null
      : await setupChannels({
          setPhoto: body.setPhoto,
          pinOverview: body.pinOverview,
          forcePin: body.forcePin,
        });

  return NextResponse.json({ ok: bot.ok, bot, channels });
}
