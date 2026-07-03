/**
 * WhatsApp (Baileys) 协议多开登录微服务。
 *
 * 为 Python 主进程（src/integrations/whatsapp_baileys_login.py）提供 HTTP 接口：
 *   POST /login/start            -> { login_id, qr_image }           发起一次扫码登录
 *   GET  /login/:id/status       -> { status, account_id, qr_image } 轮询登录状态
 *   POST /login/:id/cancel       -> { ok }                           取消/登出
 *   GET  /accounts               -> { accounts: [...] }              已连接账号
 *   GET  /health                 -> { ok: true }
 *
 * status 取值：pending | scanned | authorized | expired | failed
 * 每个登录用独立的 multi-file auth state（sessions/<login_id>/），互不干扰。
 *
 * 运行：
 *   cd services/whatsapp-baileys && npm install && PORT=8790 node server.js
 *
 * 注意：Baileys 为社区逆向库，存在 WhatsApp 封号 / ToS 风险，请配套一号一代理 + 养号。
 */

import express from "express";
import pino from "pino";
import QRCode from "qrcode";
import { fileURLToPath } from "url";
import path from "path";
import fs from "fs";
import {
  makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  downloadMediaMessage,
  proto,
} from "@whiskeysockets/baileys";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SESSIONS_DIR = process.env.WA_SESSIONS_DIR || path.join(__dirname, "sessions");
const PORT = Number(process.env.PORT || 8790);
const logger = pino({ level: process.env.LOG_LEVEL || "info" });

fs.mkdirSync(SESSIONS_DIR, { recursive: true });

/** login_id -> { sock, status, qrImage, accountId, createdAt } */
const sessions = new Map();

function newLoginId() {
  return "wa_" + Math.random().toString(36).slice(2, 10);
}

// Python 主进程统一收件箱入站桥（可选；未配置则不上报）
const PY_INGEST_URL = process.env.PY_INGEST_URL || "";
const PY_API_TOKEN = process.env.PY_API_TOKEN || "";
// P0：通讯录/会话列表同步端点——由 ingest URL 同基推导，无需额外环境变量
const PY_CONTACTS_URL = PY_INGEST_URL ? PY_INGEST_URL.replace(/\/ingest$/, "/contacts") : "";
const PY_CHATS_URL = PY_INGEST_URL ? PY_INGEST_URL.replace(/\/ingest$/, "/chats") : "";
const PY_REACTION_URL = PY_INGEST_URL ? PY_INGEST_URL.replace(/\/ingest$/, "/reaction") : "";
const PY_RECEIPT_URL = PY_INGEST_URL ? PY_INGEST_URL.replace(/\/ingest$/, "/receipt") : "";
const PY_PRESENCE_URL = PY_INGEST_URL ? PY_INGEST_URL.replace(/\/ingest$/, "/presence") : "";
const PY_MSGOP_URL = PY_INGEST_URL ? PY_INGEST_URL.replace(/\/ingest$/, "/message-op") : "";
// 同步开关（默认开）：好友名单、会话列表；会话占位上限防洪泛
const WA_SYNC_CONTACTS = String(process.env.WA_SYNC_CONTACTS ?? "1") !== "0";
const WA_SYNC_CHATS = String(process.env.WA_SYNC_CHATS ?? "1") !== "0";
const WA_CHATS_MAX = Number(process.env.WA_CHATS_MAX || 500);
// P2：群聊接入（入站显示为主，落「群组动态」；关掉即回到只私聊的旧行为）
const WA_SYNC_GROUPS = String(process.env.WA_SYNC_GROUPS ?? "1") !== "0";
// P4-3：表情回应同步（气泡显示 👍❤️；关掉即忽略 reaction 事件）
const WA_SYNC_REACTIONS = String(process.env.WA_SYNC_REACTIONS ?? "1") !== "0";
const WA_SYNC_RECEIPTS = String(process.env.WA_SYNC_RECEIPTS ?? "1") !== "0";
const WA_SYNC_PRESENCE = String(process.env.WA_SYNC_PRESENCE ?? "1") !== "0";
const WA_SYNC_EDITS = String(process.env.WA_SYNC_EDITS ?? "1") !== "0";

/** 通用 best-effort JSON POST（带鉴权头；失败只记 debug，绝不抛）。 */
async function postJson(url, payload) {
  if (!url) return;
  try {
    const headers = { "Content-Type": "application/json" };
    if (PY_API_TOKEN) headers["Authorization"] = `Bearer ${PY_API_TOKEN}`;
    await fetch(url, { method: "POST", headers, body: JSON.stringify(payload) });
  } catch (e) {
    logger.debug({ e, url }, "postJson failed");
  }
}

/** 把一条 WhatsApp 入站消息 push 到 Python 统一收件箱（best-effort）。 */
async function postIngest(payload) {
  await postJson(PY_INGEST_URL, payload);
}

/** 归一 jid：仅保留 1:1 个人号（跳过群/广播/状态）。返回裸号码或 null。 */
function personalNumber(jid) {
  const s = String(jid || "");
  if (!s || !s.endsWith("@s.whatsapp.net")) return null;
  return s.split("@")[0];
}

/** 同步平台通讯录（好友名单）到 Python。contacts 可能是数组或 {contacts:[]}。 */
async function postContacts(entry, contacts) {
  if (!WA_SYNC_CONTACTS || !PY_CONTACTS_URL || !entry.accountId) return;
  const arr = Array.isArray(contacts) ? contacts : (contacts && contacts.contacts) || [];
  const rows = [];
  for (const c of arr) {
    const num = personalNumber(c && (c.id || c.jid));
    if (!num) continue;
    const name = (c && (c.name || c.verifiedName)) || "";
    const notify = (c && c.notify) || "";
    if (!name && !notify) continue; // 无任何名字的纯号码条目不占库
    rows.push({ jid: num, name, notify });
  }
  if (!rows.length) return;
  await postJson(PY_CONTACTS_URL, {
    platform: "whatsapp", account_id: entry.accountId, contacts: rows,
  });
  logger.info({ accountId: entry.accountId, n: rows.length }, "WA contacts synced");
}

/** 主动重拉通讯录 app-state（对已恢复的老 session 补好友名单，免重扫码）。
 *  best-effort：resyncAppState 会重新触发 contacts.upsert → postContacts 落库。 */
async function resyncContacts(entry) {
  const sock = entry && entry.sock;
  if (!sock || typeof sock.resyncAppState !== "function") return;
  try {
    await sock.resyncAppState(
      ["critical_unblock_low", "regular_high", "regular_low"], false);
    logger.info({ accountId: entry.accountId }, "WA app-state resync requested");
  } catch (e) {
    logger.debug({ e }, "resyncAppState failed");
  }
}

/** P2：取群名（subject）——进程内缓存，miss 时 best-effort 拉一次 groupMetadata。 */
async function groupName(entry, jid) {
  if (!entry._groupSubjects) entry._groupSubjects = {};
  if (entry._groupSubjects[jid]) return entry._groupSubjects[jid];
  try {
    const meta = await entry.sock.groupMetadata(jid);
    const subj = (meta && meta.subject) || "";
    if (subj) entry._groupSubjects[jid] = subj;
    return subj;
  } catch (_) {
    return "";
  }
}

/** 同步平台会话列表到 Python（建会话占位）。P2：含群聊（is_group=群名/群组动态）。 */
async function postChats(entry, chats) {
  if (!WA_SYNC_CHATS || !PY_CHATS_URL || !entry.accountId) return;
  const rows = [];
  for (const ch of chats || []) {
    const jid = (ch && ch.id) || "";
    const isGroup = typeof jid === "string" && jid.endsWith("@g.us");
    if (isGroup) {
      if (!WA_SYNC_GROUPS) continue;
      const gid = jid.split("@")[0];
      const subj = (ch && ch.name) || "";
      if (subj) entry._groupSubjects = Object.assign(entry._groupSubjects || {}, { [jid]: subj });
      rows.push({
        jid: gid, name: subj, is_group: true,
        ts: Number((ch && ch.conversationTimestamp) || 0) || 0,
        unread: Number((ch && ch.unreadCount) || 0) || 0,
      });
    } else {
      const num = personalNumber(jid);
      if (!num) continue;
      rows.push({
        jid: num, name: (ch && ch.name) || "",
        ts: Number((ch && ch.conversationTimestamp) || 0) || 0,
        unread: Number((ch && ch.unreadCount) || 0) || 0,
      });
    }
    if (rows.length >= WA_CHATS_MAX) break;
  }
  if (!rows.length) return;
  await postJson(PY_CHATS_URL, {
    platform: "whatsapp", account_id: entry.accountId, chats: rows,
  });
  logger.info({ accountId: entry.accountId, n: rows.length }, "WA chats synced");
}

/** P3：老号会话列表回填（群）——重连不重放 history，用 groupFetchAllParticipating（一次
 * 拉全部所在群，非逐群，无速率突发）把「所在群」补成 群组动态 占位。私聊无同类批量 API
 * （WA 隐私），仍靠通讯录面板触达 + 消息到达时建会话。best-effort。 */
async function backfillGroups(entry) {
  if (!WA_SYNC_GROUPS || !entry.sock || !entry.accountId) return 0;
  let groups;
  try {
    groups = await entry.sock.groupFetchAllParticipating();
  } catch (e) {
    logger.debug({ e }, "groupFetchAllParticipating failed");
    return 0;
  }
  if (!entry._groupSubjects) entry._groupSubjects = {};
  const chats = [];
  for (const jid of Object.keys(groups || {})) {
    const meta = groups[jid] || {};
    const subj = meta.subject || "";
    if (subj) entry._groupSubjects[jid] = subj;
    // postChats 按 jid 末尾 @g.us 自动判群并读 .name 作群名
    chats.push({ id: jid, name: subj });
  }
  if (chats.length) await postChats(entry, chats);
  logger.info({ accountId: entry.accountId, n: chats.length }, "WA groups backfilled");
  return chats.length;
}

/** P4-3：上报一条表情回应到 Python（挂到目标 wamid）。best-effort。 */
async function postReaction(entry, r) {
  if (!WA_SYNC_REACTIONS || !PY_REACTION_URL || !entry.accountId) return;
  // messages.reaction 每项：{ key: 目标消息 key, reaction: {text, ...} }
  const key = (r && r.key) || {};
  const jid = key.remoteJid || "";
  const targetId = key.id || "";
  if (!jid || !targetId) return;
  const isGroup = typeof jid === "string" && jid.endsWith("@g.us");
  if (isGroup && !WA_SYNC_GROUPS) return;
  const chatKey = jid.split("@")[0];
  const emoji = (r && r.reaction && r.reaction.text) || ""; // 空=撤销
  // 发言人：群里用 participant，私聊/自己用 fromMe→me、否则对端号码
  let sender = "me";
  if (!key.fromMe) {
    sender = String(key.participant || "").split("@")[0] || chatKey;
  }
  await postJson(PY_REACTION_URL, {
    platform: "whatsapp", account_id: entry.accountId, chat_key: chatKey,
    target_id: String(targetId), emoji: String(emoji), sender,
    chat_type: isGroup ? "group" : "",
  });
}

// P4-4 已读回执：WAMessageStatus 枚举 → 我们的三态（SERVER_ACK=2/DELIVERY_ACK=3/READ=4/PLAYED=5）
function statusName(n) {
  const s = Number(n);
  if (s >= 4) return "read"; // READ / PLAYED
  if (s === 3) return "delivered"; // DELIVERY_ACK
  if (s === 2) return "sent"; // SERVER_ACK
  return ""; // ERROR/PENDING 不上报
}

// messages.update 每项：{ key, update: { status } }。仅出站(fromMe)消息的投递状态有意义。
async function postReceipt(entry, u) {
  if (!WA_SYNC_RECEIPTS || !PY_RECEIPT_URL || !entry.accountId) return;
  const key = (u && u.key) || {};
  if (!key.fromMe) return; // 只跟踪自己发出去的消息
  const status = statusName(u && u.update && u.update.status);
  if (!status) return;
  const jid = key.remoteJid || "";
  const targetId = key.id || "";
  if (!jid || !targetId) return;
  const isGroup = typeof jid === "string" && jid.endsWith("@g.us");
  if (isGroup && !WA_SYNC_GROUPS) return;
  const chatKey = jid.split("@")[0];
  await postJson(PY_RECEIPT_URL, {
    platform: "whatsapp", account_id: entry.accountId, chat_key: chatKey,
    target_id: String(targetId), status,
  });
}

// P4-5A 对端输入状态：presence.update → {id: jid, presences: {participant: {lastKnownPresence}}}
// 只关心私聊对端的 composing/recording/paused/available/unavailable，纯瞬态上报（不落库）。
async function postPresence(entry, update) {
  if (!WA_SYNC_PRESENCE || !PY_PRESENCE_URL || !entry.accountId) return;
  const jid = (update && update.id) || "";
  if (!jid || typeof jid !== "string") return;
  if (jid.endsWith("@g.us")) return; // 群 presence 无意义
  const chatKey = jid.split("@")[0];
  const presences = (update && update.presences) || {};
  // 私聊里 participant key 通常就是对端 jid；取任一条的 lastKnownPresence
  let state = "";
  for (const k of Object.keys(presences)) {
    const p = presences[k] || {};
    if (p.lastKnownPresence) { state = String(p.lastKnownPresence); break; }
  }
  if (!state) return;
  await postJson(PY_PRESENCE_URL, {
    platform: "whatsapp", account_id: entry.accountId, chat_key: chatKey, state,
  });
}

// P4-6A 编辑/撤回：把 protocolMessage 归一后的 op 上报 Python（改写线程内消息）。
async function postMessageOp(entry, jid, info) {
  if (!WA_SYNC_EDITS || !PY_MSGOP_URL || !entry.accountId || !info) return;
  if (!jid || typeof jid !== "string") return;
  const isGroup = jid.endsWith("@g.us");
  if (isGroup && !WA_SYNC_GROUPS) return;
  const chatKey = jid.split("@")[0];
  await postJson(PY_MSGOP_URL, {
    platform: "whatsapp", account_id: entry.accountId, chat_key: chatKey,
    target_id: info.targetId, op: info.op, text: info.text || "",
  });
}

/** 按 WhatsApp 账号号码（accountId）找到对应已授权 session。 */
function findByAccount(accountId) {
  for (const [, e] of sessions.entries()) {
    if (e.status === "authorized" && e.accountId === String(accountId)) return e;
  }
  return null;
}

/** chat_key 归一为 WhatsApp jid（裸号码 → <num>@s.whatsapp.net）。 */
function toJid(chatKey) {
  const s = String(chatKey || "");
  if (s.includes("@")) return s;
  return `${s.replace(/[^0-9]/g, "")}@s.whatsapp.net`;
}

// 首连历史回填条数（messaging-history.set）；0 关闭
const WA_BACKFILL = Number(process.env.WA_BACKFILL || 20);

// 媒体落地：写入 Python 静态目录（共享单机），前端按 /static URL 加载。未配置 WA_MEDIA_DIR 则不下载媒体。
const WA_MEDIA_DIR = process.env.WA_MEDIA_DIR || "";
const WA_MEDIA_URL_BASE = (
  process.env.WA_MEDIA_URL_BASE || "/static/protocol_media/whatsapp"
).replace(/\/$/, "");

/** 抽取一条 Baileys 消息的文本（conversation / extendedText / caption / 位置 / 名片）。 */
function extractText(msg) {
  const m = (msg && msg.message) || {};
  // 位置：转成可点击的地图链接 + 可选地名
  const loc = m.locationMessage;
  if (loc && (loc.degreesLatitude != null || loc.degreesLongitude != null)) {
    const lat = loc.degreesLatitude, lng = loc.degreesLongitude;
    const label = loc.name || loc.address || "";
    return `[位置] ${label ? label + " " : ""}https://maps.google.com/?q=${lat},${lng}`.trim();
  }
  // 名片（单个/多个）
  if (m.contactMessage) {
    return `[名片] ${m.contactMessage.displayName || ""}`.trim();
  }
  if (m.contactsArrayMessage) {
    const n = ((m.contactsArrayMessage.contacts) || []).length;
    return `[名片] ${m.contactsArrayMessage.displayName || (n + " 个联系人")}`.trim();
  }
  return (
    m.conversation ||
    (m.extendedTextMessage && m.extendedTextMessage.text) ||
    (m.imageMessage && m.imageMessage.caption) ||
    (m.videoMessage && m.videoMessage.caption) ||
    ""
  );
}

const _MEDIA_LABEL = {
  image: "[图片]", sticker: "[贴纸]", voice: "[语音]", video: "[视频]", document: "[文件]",
};

// P4-6A：从一条消息里识别「撤回 / 编辑」协议消息（protocolMessage）。
// 返回 {op:'revoke'|'edit', targetId, text?} 或 null（普通消息）。
function extractProtocolOp(msg) {
  const m = (msg && msg.message) || {};
  // 编辑在部分版本包在 editedMessage.message 里；撤回一般直接在顶层 protocolMessage
  const pm = m.protocolMessage
    || (m.editedMessage && m.editedMessage.message && m.editedMessage.message.protocolMessage);
  if (!pm || !pm.key || !pm.key.id) return null;
  const T = proto.Message.ProtocolMessage.Type;
  if (pm.type === T.REVOKE) {
    return { op: "revoke", targetId: String(pm.key.id) };
  }
  if (pm.type === T.MESSAGE_EDIT) {
    const newText = extractText({ message: pm.editedMessage || {} });
    return { op: "edit", targetId: String(pm.key.id), text: String(newText || "") };
  }
  return null;
}

/** P4-2：抽取被引用消息（quoted reply）。返回 {id,text,sender} 或 null（无引用）。
 * contextInfo 可能挂在任一消息容器下（extendedText/image/video…），逐一探测；
 * 被引用正文复用 extractText，纯媒体则回落占位标签。 */
function extractReplyTo(msg) {
  const m = (msg && msg.message) || {};
  let ctx = null;
  for (const k of Object.keys(m)) {
    const v = m[k];
    if (v && typeof v === "object" && v.contextInfo && v.contextInfo.quotedMessage) {
      ctx = v.contextInfo;
      break;
    }
  }
  if (!ctx) return null;
  const qm = ctx.quotedMessage;
  let qtext = extractText({ message: qm }) || "";
  if (!qtext) {
    const meta = waMediaMeta({ message: qm });
    if (meta) qtext = _MEDIA_LABEL[meta.kind] || "[媒体]";
  }
  const id = String(ctx.stanzaId || "");
  const sender = String(ctx.participant || "").split("@")[0] || "";
  if (!id && !qtext) return null;
  return { id, text: String(qtext).slice(0, 200), sender };
}

/** 识别 Baileys 媒体类型，返回 {kind, ext} 或 null。 */
function waMediaMeta(msg) {
  const m = (msg && msg.message) || {};
  if (m.imageMessage) return { kind: "image", ext: ".jpg" };
  if (m.stickerMessage) return { kind: "sticker", ext: ".webp" };
  if (m.audioMessage) return { kind: "voice", ext: ".ogg" };
  if (m.videoMessage) return { kind: "video", ext: ".mp4" };
  if (m.documentMessage) {
    const fn = m.documentMessage.fileName || "";
    const ext = fn.includes(".") ? fn.slice(fn.lastIndexOf(".")) : ".bin";
    return { kind: "document", ext };
  }
  return null;
}

/** 下载媒体到 WA_MEDIA_DIR，返回 {media_type, media_ref} 或 {}（失败/未配置）。 */
async function downloadWaMedia(entry, msg) {
  const meta = waMediaMeta(msg);
  if (!meta || !WA_MEDIA_DIR) return {};
  try {
    const buf = await downloadMediaMessage(
      msg, "buffer", {},
      { logger, reuploadRequest: entry.sock && entry.sock.updateMediaMessage });
    if (!buf || !buf.length) return {};
    const id = String((msg.key && msg.key.id) || Date.now()).replace(
      /[^a-zA-Z0-9_-]/g, "");
    const fname = `${entry.accountId || "wa"}_${id}${meta.ext}`;
    fs.mkdirSync(WA_MEDIA_DIR, { recursive: true });
    fs.writeFileSync(path.join(WA_MEDIA_DIR, fname), buf);
    return { media_type: meta.kind, media_ref: `${WA_MEDIA_URL_BASE}/${fname}` };
  } catch (e) {
    logger.debug({ e }, "wa media download failed");
    return {};
  }
}

/** 把一条 Baileys 入站消息 push 到 Python（skipEmpty=true 时跳过无文本无媒体，用于历史回填降噪）。 */
async function pushWaMessage(entry, msg, skipEmpty) {
  if (!msg || !msg.message) return false;
  const jid = (msg.key && msg.key.remoteJid) || "";
  if (!jid) return false;
  const isGroup = jid.endsWith("@g.us");
  if (isGroup && !WA_SYNC_GROUPS) return false; // 群聊接入关闭 → 回到只私聊
  if (!isGroup && !jid.endsWith("@s.whatsapp.net")) return false; // 广播/状态等跳过
  // fromMe：手机端/其他关联设备自己发的消息 → 镜像为出站，使会话线程两头一致。
  // 与 Python 编排器发送后的出站回写用同一 wamid 去重（INSERT OR IGNORE），不会重复。
  const fromMe = !!(msg.key && msg.key.fromMe);
  let text = extractText(msg);
  const media = await downloadWaMedia(entry, msg);
  const replyTo = extractReplyTo(msg);
  if (skipEmpty && !text && !media.media_ref) return false;
  const ts = Number(msg.messageTimestamp || 0) || Math.floor(Date.now() / 1000);
  const chatKey = jid.split("@")[0];
  let name;
  if (isGroup) {
    // 群会话名=群主题；入站正文前缀发言人名，便于坐席看清「谁在群里说话」
    name = (await groupName(entry, jid)) || chatKey;
    if (!fromMe && text) {
      const speaker = msg.pushName || (msg.key && msg.key.participant || "").split("@")[0];
      if (speaker) text = `${speaker}：${text}`;
    }
  } else {
    // 出站(fromMe)不用自己的 pushName 当会话名（会污染对端会话名）；留空由 Python 按通讯录/号码补
    name = fromMe ? "" : (msg.pushName || chatKey);
  }
  await postIngest({
    platform: "whatsapp",
    account_id: entry.accountId || "",
    chat_key: chatKey,
    name,
    text,
    ts,
    msg_id: (msg.key && msg.key.id) || "",
    direction: fromMe ? "out" : "in",
    chat_type: isGroup ? "group" : "",
    media_type: media.media_type || "",
    media_ref: media.media_ref || "",
    reply_to: replyTo || undefined,
  });
  return true;
}

/** 按代理 URL 构造 Baileys agent（一号一代理）。socks5://… 或 http(s)://… */
async function buildAgent(proxyUrl) {
  if (!proxyUrl) return undefined;
  try {
    if (proxyUrl.startsWith("socks")) {
      const { SocksProxyAgent } = await import("socks-proxy-agent");
      return new SocksProxyAgent(proxyUrl);
    }
    const { HttpsProxyAgent } = await import("https-proxy-agent");
    return new HttpsProxyAgent(proxyUrl);
  } catch (e) {
    logger.warn({ e }, "proxy agent unavailable; connecting without proxy");
    return undefined;
  }
}

async function startLogin(loginId, proxyUrl) {
  const authDir = path.join(SESSIONS_DIR, loginId);
  const { state, saveCreds } = await useMultiFileAuthState(authDir);
  const { version } = await fetchLatestBaileysVersion();
  const agent = await buildAgent(proxyUrl);

  const sock = makeWASocket({
    version,
    auth: state,
    printQRInTerminal: false,
    logger: pino({ level: "silent" }),
    agent,
    fetchAgent: agent,
    // P0：首连拉全量历史（会话列表 + 更深历史），配合 messaging-history.set 落库
    syncFullHistory: true,
  });

  const entry = {
    sock,
    status: "pending",
    qrImage: "",
    accountId: "",
    createdAt: Date.now(),
    authDir,
    proxyUrl: proxyUrl || "",
  };
  sessions.set(loginId, entry);

  sock.ev.on("creds.update", saveCreds);

  // 入站消息 → push 到 Python 统一收件箱
  sock.ev.on("messages.upsert", async (m) => {
    try {
      for (const msg of (m && m.messages) || []) {
        // P4-6A：撤回/编辑走 protocolMessage，先拦截改写线程；否则按普通消息落库
        const op = WA_SYNC_EDITS ? extractProtocolOp(msg) : null;
        if (op) {
          const jid = (msg.key && msg.key.remoteJid) || "";
          if (jid) { await postMessageOp(entry, jid, op); continue; }
        }
        await pushWaMessage(entry, msg, false);
      }
    } catch (e) {
      logger.debug({ e }, "messages.upsert handler failed");
    }
  });

  // P2 群名缓存：groups.upsert/update 携带 subject → 缓存供入站群消息取群名
  if (WA_SYNC_GROUPS) {
    const _cacheGroups = (arr) => {
      if (!entry._groupSubjects) entry._groupSubjects = {};
      for (const g of arr || []) {
        if (g && g.id && g.subject) entry._groupSubjects[g.id] = g.subject;
      }
    };
    sock.ev.on("groups.upsert", (arr) => { try { _cacheGroups(arr); } catch (_) {} });
    sock.ev.on("groups.update", (arr) => { try { _cacheGroups(arr); } catch (_) {} });
  }

  // P3 会话列表保鲜：新会话通知(chats.upsert)→建占位（私聊+群皆可，best-effort）。
  // 不监听 chats.update（仅时间戳/未读变动，过于频繁；消息到达已由 messages.upsert 落库）。
  if (WA_SYNC_CHATS) {
    sock.ev.on("chats.upsert", async (arr) => {
      try { await postChats(entry, arr); } catch (_) {}
    });
  }

  // P4-3 表情回应：messages.reaction → 挂到目标消息（气泡显示 👍❤️）
  if (WA_SYNC_REACTIONS) {
    sock.ev.on("messages.reaction", async (arr) => {
      try {
        for (const r of arr || []) await postReaction(entry, r);
      } catch (_) {}
    });
  }

  // P4-4 已读回执：messages.update 携带 status → 出站气泡 ✓/✓✓/✓✓蓝
  if (WA_SYNC_RECEIPTS) {
    sock.ev.on("messages.update", async (arr) => {
      try {
        for (const u of arr || []) await postReceipt(entry, u);
      } catch (_) {}
    });
  }

  // P4-5A 对端输入状态：presence.update → 会话头「对方正在输入…」（需先 presenceSubscribe）
  if (WA_SYNC_PRESENCE) {
    sock.ev.on("presence.update", async (update) => {
      try { await postPresence(entry, update); } catch (_) {}
    });
  }

  // P0 好友名单同步：通讯录首次批量(contacts.set) + 增量(upsert/update)
  if (WA_SYNC_CONTACTS) {
    sock.ev.on("contacts.set", async (c) => {
      try { await postContacts(entry, c); }
      catch (e) { logger.debug({ e }, "contacts.set handler failed"); }
    });
    sock.ev.on("contacts.upsert", async (c) => {
      try { await postContacts(entry, c); }
      catch (e) { logger.debug({ e }, "contacts.upsert handler failed"); }
    });
    sock.ev.on("contacts.update", async (c) => {
      try { await postContacts(entry, c); }
      catch (e) { logger.debug({ e }, "contacts.update handler failed"); }
    });
  }

  // 历史同步：messaging-history.set 携带初次同步的会话列表(chats)与消息(messages)，
  // 以及 P1 按需拉取(fetchMessageHistory)回流的更早消息(syncType=ON_DEMAND)。
  // 恒挂：on-demand 回填即使 WA_BACKFILL=0 也需落库。
  sock.ev.on("messaging-history.set", async (h) => {
    try {
      // P0 全量会话列表：把会话建为占位（无消息也可见，贴近官方）
      if (WA_SYNC_CHATS && h && Array.isArray(h.chats)) {
        await postChats(entry, h.chats);
      }
      if (h && Array.isArray(h.messages)) {
        const onDemand = h.syncType === 5; // proto.HistorySync.HistorySyncType.ON_DEMAND
        if (onDemand || WA_BACKFILL > 0) {
          // 按需回填拉全量；初次同步只取末尾 WA_BACKFILL 条降噪
          const msgs = onDemand ? h.messages : h.messages.slice(-WA_BACKFILL);
          for (const msg of msgs) {
            await pushWaMessage(entry, msg, true); // 跳过无文本，降噪
          }
        }
      }
    } catch (e) {
      logger.debug({ e }, "messaging-history.set handler failed");
    }
  });

  sock.ev.on("connection.update", async (update) => {
    const { connection, lastDisconnect, qr } = update;
    if (qr) {
      try {
        entry.qrImage = await QRCode.toDataURL(qr, { width: 240, margin: 1 });
      } catch (e) {
        logger.error({ e }, "qr encode failed");
      }
    }
    if (connection === "open") {
      entry.status = "authorized";
      try {
        entry.accountId = (sock.user && (sock.user.id || "").split(":")[0]) || "";
      } catch (_) {
        entry.accountId = "";
      }
      logger.info({ loginId, accountId: entry.accountId }, "WA connected");
      // P0 自愈：恢复的老 session 不会重放初次 contacts.set，(re)连成功后主动补拉一次
      // 通讯录 app-state（幂等，best-effort）——好友名单不必重扫码即可回流。只做一次。
      if (WA_SYNC_CONTACTS && !entry._resynced) {
        entry._resynced = true;
        resyncContacts(entry).catch(() => {});
      }
      // P3 自愈：老号重连补建「所在群」会话占位（一次；群组动态即刻可见，不必干等消息）
      if (WA_SYNC_GROUPS && !entry._groupsBackfilled) {
        entry._groupsBackfilled = true;
        backfillGroups(entry).catch(() => {});
      }
    } else if (connection === "close") {
      const code =
        (lastDisconnect &&
          lastDisconnect.error &&
          lastDisconnect.error.output &&
          lastDisconnect.error.output.statusCode) ||
        0;
      if (code === DisconnectReason.restartRequired) {
        // 登录成功后 Baileys 要求重启 socket —— 重新拉起以维持连接（沿用同一代理）
        startLogin(loginId, entry.proxyUrl).catch((e) =>
          logger.error({ e }, "restart failed"));
      } else if (code === DisconnectReason.loggedOut) {
        entry.status = "failed";
      } else if (entry.status !== "authorized") {
        entry.status = "expired";
      }
    }
  });

  return entry;
}

/** 恢复磁盘上已持久化的所有 session（开机 / 主动调用，幂等）。 */
async function restoreAll() {
  let dirs = [];
  try {
    dirs = fs
      .readdirSync(SESSIONS_DIR, { withFileTypes: true })
      .filter((d) => d.isDirectory())
      .map((d) => d.name);
  } catch (_) {
    dirs = [];
  }
  let restored = 0;
  for (const loginId of dirs) {
    if (sessions.has(loginId)) continue; // 已在内存
    try {
      await startLogin(loginId);
      restored += 1;
    } catch (e) {
      logger.warn({ e, loginId }, "restore session failed");
    }
  }
  return restored;
}

const app = express();
app.use(express.json());

app.get("/health", (_req, res) => res.json({ ok: true }));

app.post("/accounts/restore", async (_req, res) => {
  const restored = await restoreAll();
  res.json({ ok: true, restored });
});

app.post("/login/start", async (req, res) => {
  try {
    const loginId = newLoginId();
    const proxyUrl = (req.body && req.body.proxy_url) || "";
    const entry = await startLogin(loginId, proxyUrl);
    // 等待首个 QR（最多 ~8s）
    const deadline = Date.now() + 8000;
    while (!entry.qrImage && entry.status === "pending" && Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 200));
    }
    res.json({ login_id: loginId, qr_image: entry.qrImage, status: entry.status });
  } catch (e) {
    logger.error({ e }, "start failed");
    res.status(500).json({ error: String(e) });
  }
});

app.get("/login/:id/status", (req, res) => {
  const entry = sessions.get(req.params.id);
  if (!entry) return res.json({ status: "expired", detail: "session not found" });
  res.json({
    status: entry.status,
    account_id: entry.accountId,
    qr_image: entry.status === "authorized" ? "" : entry.qrImage,
  });
});

app.post("/login/:id/cancel", async (req, res) => {
  const entry = sessions.get(req.params.id);
  if (entry) {
    try {
      if (entry.sock) await entry.sock.logout().catch(() => {});
    } catch (_) {}
    sessions.delete(req.params.id);
  }
  res.json({ ok: true });
});

// P1：按需拉取更早历史——以 store 最旧消息为锚点，向手机补拉。
// 回流经 messaging-history.set(syncType=ON_DEMAND) 落库，非同步返回。
app.post("/accounts/:id/history", async (req, res) => {
  const entry = findByAccount(req.params.id);
  if (!entry || !entry.sock) {
    return res.status(404).json({ ok: false, error: "account not connected" });
  }
  const jid = toJid((req.body && req.body.jid) || "");
  const count = Math.max(1, Math.min(200, Number((req.body && req.body.count) || 50)));
  const oldestId = String((req.body && req.body.oldest_id) || "");
  const oldestTs = Number((req.body && req.body.oldest_ts) || 0);
  const fromMe = !!(req.body && req.body.from_me);
  if (!jid || !oldestId) {
    return res.status(400).json({ ok: false, error: "jid and oldest_id required" });
  }
  if (typeof entry.sock.fetchMessageHistory !== "function") {
    return res.status(501).json({ ok: false, error: "fetchMessageHistory unavailable" });
  }
  try {
    const key = { remoteJid: jid, id: oldestId, fromMe };
    const reqId = await entry.sock.fetchMessageHistory(count, key, oldestTs);
    logger.info({ accountId: entry.accountId, jid, count }, "WA history fetch requested");
    res.json({ ok: true, request_id: String(reqId || "") });
  } catch (e) {
    logger.error({ e }, "fetchMessageHistory failed");
    res.status(500).json({ ok: false, error: String(e) });
  }
});

// P2：取头像 URL（单个 jid；无头像/私密返回空 url 而非报错，避免前端把「没头像」当失败）
app.get("/accounts/:id/avatar", async (req, res) => {
  const entry = findByAccount(req.params.id);
  if (!entry || !entry.sock) {
    return res.status(404).json({ ok: false, error: "account not connected" });
  }
  const jid = toJid(req.query.jid || "");
  if (!jid) return res.status(400).json({ ok: false, error: "jid required" });
  try {
    const url = await entry.sock.profilePictureUrl(jid, "image");
    res.json({ ok: true, url: url || "" });
  } catch (e) {
    res.json({ ok: true, url: "" }); // 无头像/隐私设置 → 空
  }
});

// P0：手动补拉通讯录（好友名单）——对已登录的老号免重扫码回流联系人
app.post("/accounts/:id/resync", async (req, res) => {
  const entry = findByAccount(req.params.id);
  if (!entry || !entry.sock) {
    return res.status(404).json({ ok: false, error: "account not connected" });
  }
  entry._resynced = true;
  resyncContacts(entry).catch(() => {});
  res.json({ ok: true });
});

// P3：手动补建「所在群」会话占位（老号重连后一键回填群组动态）
app.post("/accounts/:id/sync-groups", async (req, res) => {
  const entry = findByAccount(req.params.id);
  if (!entry || !entry.sock) {
    return res.status(404).json({ ok: false, error: "account not connected" });
  }
  try {
    const n = await backfillGroups(entry);
    res.json({ ok: true, groups: n });
  } catch (e) {
    res.status(500).json({ ok: false, error: String(e) });
  }
});

// P4-5A：订阅对端在线/输入状态（打开会话时调；之后 presence.update 才会回流 typing）
app.post("/accounts/:id/subscribe-presence", async (req, res) => {
  const entry = findByAccount(req.params.id);
  if (!entry || !entry.sock) {
    return res.status(404).json({ ok: false, error: "account not connected" });
  }
  const jid = toJid((req.body && req.body.jid) || "");
  if (!jid) return res.status(400).json({ ok: false, error: "jid required" });
  try {
    await entry.sock.presenceSubscribe(jid);
    res.json({ ok: true });
  } catch (e) {
    logger.debug({ e }, "presenceSubscribe failed");
    res.status(500).json({ ok: false, error: String(e) });
  }
});

// P4-5B：把 {id,from_me,participant,text} 组装成 Baileys sendMessage 的 quoted 选项。
// 只需最小 WAMessage（key + 一段 conversation 文本）即可让对端看到「回复引用条」。
function buildQuoted(jid, quoted) {
  if (!quoted || !quoted.id) return undefined;
  const qkey = { remoteJid: jid, id: String(quoted.id), fromMe: !!quoted.from_me };
  const participant = quoted.participant || "";
  if (participant) {
    qkey.participant = String(participant).includes("@")
      ? participant : `${participant}@s.whatsapp.net`;
  }
  return { key: qkey, message: { conversation: String(quoted.text || "") } };
}

app.post("/accounts/:id/send", async (req, res) => {
  const entry = findByAccount(req.params.id);
  if (!entry || !entry.sock) {
    return res.status(404).json({ ok: false, error: "account not connected" });
  }
  const jid = toJid((req.body && req.body.jid) || "");
  const text = String((req.body && req.body.text) || "");
  if (!jid || !text) {
    return res.status(400).json({ ok: false, error: "jid and text required" });
  }
  // P4-5B 引用回复：body.quoted={id,from_me,participant,text} → 带原生引用发送
  const quotedMsg = buildQuoted(jid, req.body && req.body.quoted);
  const sendOpts = quotedMsg ? { quoted: quotedMsg } : undefined;
  try {
    const sent = await entry.sock.sendMessage(jid, { text }, sendOpts);
    res.json({ ok: true, message_id: (sent && sent.key && sent.key.id) || "" });
  } catch (e) {
    logger.error({ e }, "send failed");
    res.status(500).json({ ok: false, error: String(e) });
  }
});

// P4-5B：坐席给某条消息发表情回应（emoji 空串=撤销）。target key 需还原原消息 key。
app.post("/accounts/:id/react", async (req, res) => {
  const entry = findByAccount(req.params.id);
  if (!entry || !entry.sock) {
    return res.status(404).json({ ok: false, error: "account not connected" });
  }
  const jid = toJid((req.body && req.body.jid) || "");
  const targetId = String((req.body && req.body.target_id) || "");
  const emoji = String((req.body && req.body.emoji) || ""); // 空=撤销
  if (!jid || !targetId) {
    return res.status(400).json({ ok: false, error: "jid and target_id required" });
  }
  const key = { remoteJid: jid, id: targetId, fromMe: !!(req.body && req.body.from_me) };
  const participant = (req.body && req.body.participant) || "";
  if (participant) {
    key.participant = String(participant).includes("@")
      ? participant : `${participant}@s.whatsapp.net`;
  }
  try {
    await entry.sock.sendMessage(jid, { react: { text: emoji, key } });
    res.json({ ok: true });
  } catch (e) {
    logger.error({ e }, "react failed");
    res.status(500).json({ ok: false, error: String(e) });
  }
});

// P4-6B：坐席主动编辑/撤回自己发出的消息（仅 fromMe；WhatsApp 有时间窗，越界由 WA 拒绝）。
app.post("/accounts/:id/message-op", async (req, res) => {
  const entry = findByAccount(req.params.id);
  if (!entry || !entry.sock) {
    return res.status(404).json({ ok: false, error: "account not connected" });
  }
  const jid = toJid((req.body && req.body.jid) || "");
  const targetId = String((req.body && req.body.target_id) || "");
  const op = String((req.body && req.body.op) || "");
  if (!jid || !targetId || !op) {
    return res.status(400).json({ ok: false, error: "jid, target_id, op required" });
  }
  const key = { remoteJid: jid, id: targetId, fromMe: true };
  try {
    if (op === "revoke") {
      await entry.sock.sendMessage(jid, { delete: key });
    } else if (op === "edit") {
      const text = String((req.body && req.body.text) || "");
      if (!text) return res.status(400).json({ ok: false, error: "text required for edit" });
      await entry.sock.sendMessage(jid, { text, edit: key });
    } else {
      return res.status(400).json({ ok: false, error: "unknown op" });
    }
    res.json({ ok: true });
  } catch (e) {
    logger.error({ e }, "message-op failed");
    res.status(500).json({ ok: false, error: String(e) });
  }
});

app.post("/accounts/:id/send-media", async (req, res) => {
  const entry = findByAccount(req.params.id);
  if (!entry || !entry.sock) {
    return res.status(404).json({ ok: false, error: "account not connected" });
  }
  const jid = toJid((req.body && req.body.jid) || "");
  const mpath = String((req.body && req.body.path) || "");
  const mtype = String((req.body && req.body.media_type) || "document");
  const caption = String((req.body && req.body.caption) || "");
  if (!jid || !mpath) {
    return res.status(400).json({ ok: false, error: "jid and path required" });
  }
  try {
    const buf = fs.readFileSync(mpath);
    let content;
    if (mtype === "image") content = { image: buf, caption };
    else if (mtype === "voice") {
      content = { audio: buf, ptt: true, mimetype: "audio/ogg; codecs=opus" };
    } else if (mtype === "video") content = { video: buf, caption };
    else {
      content = {
        document: buf,
        fileName: path.basename(mpath),
        caption,
      };
    }
    const sent = await entry.sock.sendMessage(jid, content);
    res.json({ ok: true, message_id: (sent && sent.key && sent.key.id) || "" });
  } catch (e) {
    logger.error({ e }, "send-media failed");
    res.status(500).json({ ok: false, error: String(e) });
  }
});

// 登出账号：解除手机端设备关联 + 清掉本地持久化 session（开机不再自动恢复）。
// 幂等：账号不在线（无 sock）也返回 ok，仅尽量清理磁盘残留。
app.post("/accounts/:id/logout", async (req, res) => {
  const accountId = String(req.params.id);
  let loginId = "";
  let entry = null;
  for (const [id, e] of sessions.entries()) {
    if (e.accountId === accountId) { loginId = id; entry = e; break; }
  }
  try {
    if (entry && entry.sock) await entry.sock.logout().catch(() => {});
  } catch (_) {}
  if (loginId) sessions.delete(loginId);
  // 清磁盘 session 目录（authDir 或按 loginId 兜底）→ 防 restoreAll 复活
  try {
    const dir = (entry && entry.authDir) ||
      (loginId ? path.join(SESSIONS_DIR, loginId) : "");
    if (dir && fs.existsSync(dir)) fs.rmSync(dir, { recursive: true, force: true });
  } catch (e) {
    logger.debug({ e }, "logout session dir cleanup failed");
  }
  res.json({ ok: true, account_id: accountId });
});

app.get("/accounts", (_req, res) => {
  const accounts = [];
  for (const [id, e] of sessions.entries()) {
    if (e.status === "authorized") {
      accounts.push({ login_id: id, account_id: e.accountId });
    }
  }
  res.json({ accounts });
});

app.listen(PORT, async () => {
  logger.info(`WA Baileys login service on :${PORT} (sessions: ${SESSIONS_DIR})`);
  // 开机自动恢复已登录账号 → 多账号 7×24 在线
  try {
    const restored = await restoreAll();
    logger.info(`restored ${restored} persisted WhatsApp session(s) on boot`);
  } catch (e) {
    logger.error({ e }, "boot restore failed");
  }
});
