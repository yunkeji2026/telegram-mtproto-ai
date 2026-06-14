import { readFile } from "fs/promises";
import path from "path";
import { TELEGRAM_CHANNEL, TELEGRAM_GROUP, SITE_URL, MINIAPP_URL, BOT_URL, CONTACT_URL } from "./site";
import { buildOverviewPost } from "./catalog-posts";

export type BroadcastTarget = "channel" | "group" | "both";
export interface BroadcastResult {
  chat: string;
  ok: boolean;
  error?: string;
  messageId?: number;
}

function richButtons() {
  return {
    inline_keyboard: [
      [
        { text: "🌐 官网", url: SITE_URL },
        { text: "📱 小程序", url: MINIAPP_URL },
      ],
      [
        { text: "🤖 机器人", url: BOT_URL },
        { text: "👤 客服", url: CONTACT_URL },
      ],
    ],
  };
}

async function callApi(token: string, method: string, body: Record<string, unknown>) {
  const res = await fetch(`https://api.telegram.org/bot${token}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}

async function postTo(token: string, chat: string, text: string, withButton: boolean): Promise<BroadcastResult> {
  const body: Record<string, unknown> = {
    chat_id: chat,
    text,
    parse_mode: "HTML",
    disable_web_page_preview: true,
  };
  if (withButton) body.reply_markup = richButtons();
  try {
    const data = await callApi(token, "sendMessage", body);
    return {
      chat,
      ok: Boolean(data?.ok),
      error: data?.ok ? undefined : data?.description,
      messageId: data?.result?.message_id,
    };
  } catch (e) {
    return { chat, ok: false, error: String(e) };
  }
}

// photo can be an https URL (Telegram fetches) or a local file path (we upload via multipart).
// Multipart upload is preferred for large assets to avoid Telegram's fetch timeout.
async function photoTo(
  token: string,
  chat: string,
  photo: string,
  caption: string,
  withButton: boolean
): Promise<BroadcastResult> {
  const isUrl = /^https?:\/\//i.test(photo);
  try {
    let data: { ok?: boolean; description?: string; result?: { message_id?: number } };
    if (isUrl) {
      const body: Record<string, unknown> = { chat_id: chat, photo, caption, parse_mode: "HTML" };
      if (withButton) body.reply_markup = richButtons();
      data = await callApi(token, "sendPhoto", body);
    } else {
      const buf = await readFile(photo);
      const form = new FormData();
      form.append("chat_id", chat);
      form.append("caption", caption);
      form.append("parse_mode", "HTML");
      if (withButton) form.append("reply_markup", JSON.stringify(richButtons()));
      form.append("photo", new Blob([new Uint8Array(buf)]), path.basename(photo));
      const res = await fetch(`https://api.telegram.org/bot${token}/sendPhoto`, {
        method: "POST",
        body: form,
      });
      data = await res.json();
    }
    return {
      chat,
      ok: Boolean(data?.ok),
      error: data?.ok ? undefined : data?.description,
      messageId: data?.result?.message_id,
    };
  } catch (e) {
    return { chat, ok: false, error: String(e) };
  }
}

export function targetChats(target: BroadcastTarget): string[] {
  const out: string[] = [];
  if (target === "channel" || target === "both") out.push(`@${TELEGRAM_CHANNEL}`);
  if (target === "group" || target === "both") out.push(`@${TELEGRAM_GROUP}`);
  return out;
}

/** Send a message to the channel/group. Returns per-target results. */
export async function broadcastMessage(opts: {
  text: string;
  target: BroadcastTarget;
  withButton: boolean;
}): Promise<{ ok: boolean; results: BroadcastResult[] }> {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) return { ok: false, results: [{ chat: "-", ok: false, error: "no_bot_token" }] };
  const chats = targetChats(opts.target);
  const results = await Promise.all(chats.map((c) => postTo(token, c, opts.text, opts.withButton)));
  return { ok: results.length > 0 && results.every((r) => r.ok), results };
}

/** Send a photo with caption + buttons to the channel/group. */
export async function broadcastPhoto(opts: {
  photo: string;
  caption: string;
  target: BroadcastTarget;
  withButton: boolean;
}): Promise<{ ok: boolean; results: BroadcastResult[] }> {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) return { ok: false, results: [{ chat: "-", ok: false, error: "no_bot_token" }] };
  const chats = targetChats(opts.target);
  const results = await Promise.all(
    chats.map((c) => photoTo(token, c, opts.photo, opts.caption, opts.withButton))
  );
  return { ok: results.length > 0 && results.every((r) => r.ok), results };
}

export async function pinMessage(chat: string, messageId: number, silent = true): Promise<boolean> {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) return false;
  try {
    const data = await callApi(token, "pinChatMessage", {
      chat_id: chat,
      message_id: messageId,
      disable_notification: silent,
    });
    return Boolean(data?.ok);
  } catch {
    return false;
  }
}

export async function deleteMessage(chat: string, messageId: number): Promise<boolean> {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) return false;
  try {
    const data = await callApi(token, "deleteMessage", { chat_id: chat, message_id: messageId });
    return Boolean(data?.ok);
  } catch {
    return false;
  }
}

// ── 频道 / 群 品牌信息 ────────────────────────────────────────────────
// 显示名 + 简介。需要 Bot 是该频道/群的管理员（有「修改群信息」权限）。
// username（@hykj7 / @hykjz）属平台实体，不在此改动（保留以免断历史外链）。

export const CHANNEL_BRAND = {
  channel: {
    title: "无界科技 BOUNDLESS · 官方频道",
    description:
      "无界科技官方频道 · 让沟通，无界。" +
      "🎭换脸 🎙克隆声音 🎬直播换脸换声 🌐实时换语言 💬AI自动成交 🔐私有部署。" +
      "真实案例 · 新功能 · 限时优惠第一时间发布 · USDT 结算。官网与客服见置顶。",
  },
  group: {
    title: "无界科技 · 交流群",
    description:
      "无界科技官方交流群 · 换脸/克隆声音/直播分身/实时换语言/AI 自动成交。" +
      "提问、领试用、同行交流。@小界 或点客服随时响应；广告与刷屏将被移除。",
  },
} as const;

// 设置频道/群头像。需要 Bot 是管理员且有「修改群信息」权限。photo 为本地文件路径，multipart 上传。
async function setChatPhoto(token: string, chat: string, photoPath: string): Promise<BroadcastResult> {
  try {
    const buf = await readFile(photoPath);
    const form = new FormData();
    form.append("chat_id", chat);
    form.append("photo", new Blob([new Uint8Array(buf)]), path.basename(photoPath));
    const res = await fetch(`https://api.telegram.org/bot${token}/setChatPhoto`, {
      method: "POST",
      body: form,
    });
    const data = await res.json();
    return { chat: `${chat} (photo)`, ok: Boolean(data?.ok), error: data?.ok ? undefined : data?.description };
  } catch (e) {
    return { chat: `${chat} (photo)`, ok: false, error: String(e) };
  }
}

async function setChatMeta(
  token: string,
  chat: string,
  title: string,
  description: string
): Promise<BroadcastResult> {
  try {
    // setChatDescription 上限约 255 字符；标题上限 128。
    const t = await callApi(token, "setChatTitle", { chat_id: chat, title: title.slice(0, 128) });
    const d = await callApi(token, "setChatDescription", {
      chat_id: chat,
      description: description.slice(0, 255),
    });
    // 幂等：标题/简介未变化时 Telegram 返回 "...is not modified"，视为成功。
    const tDesc = String((t as { description?: string })?.description || "");
    const dDesc = String((d as { description?: string })?.description || "");
    const okT = Boolean(t?.ok) || /is not modified/i.test(tDesc);
    const okD = Boolean(d?.ok) || /is not modified/i.test(dDesc);
    const ok = okT && okD;
    return {
      chat,
      ok,
      error: ok ? undefined : tDesc || dDesc || "set_meta_failed",
    };
  } catch (e) {
    return { chat, ok: false, error: String(e) };
  }
}

/** 无界科技品牌头像（深底圆裁友好）本地路径，供频道/群 setChatPhoto 使用。
 *  注：当前沿用过渡期资源 hualing-avatar.png，新「界」字头像在视觉重制阶段替换。 */
function brandAvatarPath(): string {
  return path.join(process.cwd(), "public", "brand", "logos", "hualing-avatar.png");
}

/** Set channel & group display name + description (+ avatar, + pinned overview).
 *  Bot must be an admin of each chat with "change info" rights for title/description/photo.
 *  Failures are returned per-target, never thrown. */
export async function setupChannels(opts?: {
  pinOverview?: boolean;
  setPhoto?: boolean;
  forcePin?: boolean;
}): Promise<{
  ok: boolean;
  results: BroadcastResult[];
}> {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) return { ok: false, results: [{ chat: "-", ok: false, error: "no_bot_token" }] };

  const results: BroadcastResult[] = [];
  results.push(
    await setChatMeta(
      token,
      `@${TELEGRAM_CHANNEL}`,
      CHANNEL_BRAND.channel.title,
      CHANNEL_BRAND.channel.description
    )
  );
  results.push(
    await setChatMeta(
      token,
      `@${TELEGRAM_GROUP}`,
      CHANNEL_BRAND.group.title,
      CHANNEL_BRAND.group.description
    )
  );

  if (opts?.setPhoto !== false) {
    const avatar = brandAvatarPath();
    results.push(await setChatPhoto(token, `@${TELEGRAM_CHANNEL}`, avatar));
    results.push(await setChatPhoto(token, `@${TELEGRAM_GROUP}`, avatar));
  }

  if (opts?.pinOverview !== false) {
    // 幂等：若频道当前置顶已是本概览贴（按 caption 首行签名识别），默认跳过，避免重跑刷屏。
    // 频道消息没有 from.id（以 sender_chat 归属频道），故用内容签名而非发送者判断。
    // forcePin=true 可强制重发并置顶。
    const overview = buildOverviewPost("zh");
    const sig = overview.caption.split("\n")[0].replace(/<[^>]+>/g, "").trim();
    let alreadyPinned = false;
    if (!opts?.forcePin) {
      try {
        const chat = await callApi(token, "getChat", { chat_id: `@${TELEGRAM_CHANNEL}` });
        const pinned = (chat as { result?: { pinned_message?: { photo?: unknown; caption?: string } } })
          ?.result?.pinned_message;
        alreadyPinned = Boolean(
          pinned?.photo && typeof pinned?.caption === "string" && sig && pinned.caption.includes(sig)
        );
      } catch {
        /* 查询失败则退回到正常发布逻辑 */
      }
    }

    if (alreadyPinned) {
      results.push({ chat: `@${TELEGRAM_CHANNEL} (overview: already pinned, skipped)`, ok: true });
    } else {
      const posted = await photoTo(token, `@${TELEGRAM_CHANNEL}`, overview.imagePath, overview.caption, true);
      results.push({ ...posted, chat: `@${TELEGRAM_CHANNEL} (overview)` });
      if (posted.ok && posted.messageId) {
        await pinMessage(`@${TELEGRAM_CHANNEL}`, posted.messageId, true);
      }
    }
  }

  return { ok: results.every((r) => r.ok), results };
}
