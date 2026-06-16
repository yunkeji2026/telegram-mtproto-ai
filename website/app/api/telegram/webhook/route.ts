import { NextRequest, NextResponse } from "next/server";
import { detectLang } from "@/lib/bot-knowledge";
import {
  handleCallback,
  handleCommand,
  handleFreeText,
  handleGroupMessage,
  sendText,
} from "@/lib/telegram-bot";
import { bindAdminChat, unbindAdminChat } from "@/lib/admin-store";
import { isDuplicateUpdate } from "@/lib/tg-dedup";
import { BOT_HANDLE, TELEGRAM_GROUP } from "@/lib/site";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

type TgUpdate = {
  update_id?: number;
  message?: {
    message_id?: number;
    chat: { id: number; type?: string; username?: string };
    text?: string;
    from?: { language_code?: string };
    reply_to_message?: { from?: { username?: string; is_bot?: boolean } };
    entities?: { type: string; offset: number; length: number }[];
  };
  callback_query?: {
    id: string;
    data?: string;
    message?: { chat: { id: number }; message_id?: number };
    from?: { language_code?: string; id: number; username?: string; first_name?: string };
  };
};

const BOT_AT = `@${BOT_HANDLE}`.toLowerCase();

/** Groups we will answer in: the configured public group + optional id allowlist. */
function isAllowedGroup(chat: { username?: string; id: number }): boolean {
  if (chat.username && chat.username.toLowerCase() === TELEGRAM_GROUP.toLowerCase()) return true;
  const ids = (process.env.TELEGRAM_GROUP_IDS || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  return ids.includes(String(chat.id));
}

export async function POST(req: NextRequest) {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) return NextResponse.json({ ok: true });

  const secret = process.env.TELEGRAM_WEBHOOK_SECRET;
  if (secret) {
    const hdr = req.headers.get("x-telegram-bot-api-secret-token");
    if (hdr !== secret) {
      return NextResponse.json({ ok: false }, { status: 403 });
    }
  }

  let update: TgUpdate;
  try {
    update = await req.json();
  } catch {
    return NextResponse.json({ ok: true });
  }

  // idempotency: ignore Telegram retries of an already-processed update
  if (isDuplicateUpdate(update.update_id)) {
    return NextResponse.json({ ok: true });
  }

  try {
    if (update.callback_query) {
      const cq = update.callback_query;
      const chatId = cq.message?.chat.id;
      const data = cq.data ?? "";
      if (chatId && data) {
        const lang = detectLang(cq.from?.language_code);
        const from = cq.from
          ? { id: cq.from.id, username: cq.from.username, first_name: cq.from.first_name }
          : undefined;
        await handleCallback(chatId, data, cq.id, lang, from, cq.message?.message_id);
      }
      return NextResponse.json({ ok: true });
    }

    const msg = update.message;
    if (!msg?.text || !msg.chat?.id) {
      return NextResponse.json({ ok: true });
    }

    const chatId = msg.chat.id;
    const text = msg.text.trim();
    const lang = detectLang(msg.from?.language_code);
    const chatType = msg.chat.type ?? "private";

    // ── group / supergroup: answer only when invoked, never flood ──
    if (chatType === "group" || chatType === "supergroup") {
      if (!isAllowedGroup(msg.chat)) {
        return NextResponse.json({ ok: true });
      }
      const lower = text.toLowerCase();
      const repliedToBot =
        msg.reply_to_message?.from?.is_bot === true &&
        msg.reply_to_message?.from?.username?.toLowerCase() === BOT_HANDLE.toLowerCase();
      const mentioned = lower.includes(BOT_AT);
      const isCmd = text.startsWith("/");
      // command must target our bot (or be untargeted) to avoid hijacking other bots
      const cmdForUs = isCmd && (lower.includes(BOT_AT) || !lower.includes("@"));

      if (!(mentioned || repliedToBot || (isCmd && cmdForUs))) {
        return NextResponse.json({ ok: true });
      }

      // strip @botname mention and any leading /command → clean question
      const question = text
        .replace(new RegExp(BOT_AT, "gi"), "")
        .replace(/^\/[a-z0-9_]+/i, "")
        .trim();

      // NOTE: web_app buttons are invalid in groups; handleGroupMessage uses url-only.
      await handleGroupMessage(chatId, question, lang, msg.message_id);
      return NextResponse.json({ ok: true });
    }

    // admin self-binding for lead notifications
    if (text.startsWith("/bindadmin")) {
      const arg = text.split(/\s+/)[1] ?? "";
      const key = process.env.TELEGRAM_SETUP_KEY;
      if (key && arg === key) {
        const added = await bindAdminChat(chatId);
        await sendText(
          chatId,
          added
            ? `✅ 已绑定为留资接收人\nchat_id: <code>${chatId}</code>\n以后有人留资会推送到这里。`
            : `ℹ️ 你已经是留资接收人了（chat_id: <code>${chatId}</code>）。`
        );
      } else {
        await sendText(chatId, "❌ 口令错误。用法：/bindadmin <setup_key>");
      }
      return NextResponse.json({ ok: true });
    }
    if (text.startsWith("/unbindadmin")) {
      await unbindAdminChat(chatId);
      await sendText(chatId, "✅ 已取消留资推送（本会话）。");
      return NextResponse.json({ ok: true });
    }

    if (text.startsWith("/")) {
      const [cmd, ...rest] = text.split(/\s+/);
      const startArg = cmd.startsWith("/start") && rest[0] ? rest[0] : undefined;
      await handleCommand(chatId, cmd, lang, startArg);
    } else {
      await handleFreeText(chatId, text, lang);
    }
  } catch {
    /* never fail webhook — TG will retry */
  }

  return NextResponse.json({ ok: true });
}
