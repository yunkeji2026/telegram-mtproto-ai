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

/** 把一条 WhatsApp 入站消息 push 到 Python 统一收件箱（best-effort）。 */
async function postIngest(payload) {
  if (!PY_INGEST_URL) return;
  try {
    const headers = { "Content-Type": "application/json" };
    if (PY_API_TOKEN) headers["Authorization"] = `Bearer ${PY_API_TOKEN}`;
    await fetch(PY_INGEST_URL, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
    });
  } catch (e) {
    logger.debug({ e }, "ingest push failed");
  }
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

/** 抽取一条 Baileys 消息的文本（conversation / extendedText / 图片 caption）。 */
function extractText(msg) {
  const m = (msg && msg.message) || {};
  return (
    m.conversation ||
    (m.extendedTextMessage && m.extendedTextMessage.text) ||
    (m.imageMessage && m.imageMessage.caption) ||
    (m.videoMessage && m.videoMessage.caption) ||
    ""
  );
}

/** 识别 Baileys 媒体类型，返回 {kind, ext} 或 null。 */
function waMediaMeta(msg) {
  const m = (msg && msg.message) || {};
  if (m.imageMessage) return { kind: "image", ext: ".jpg" };
  if (m.stickerMessage) return { kind: "image", ext: ".webp" };
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
  if (msg.key && msg.key.fromMe) return false; // 出站由 Python 侧回写
  const jid = (msg.key && msg.key.remoteJid) || "";
  if (!jid || jid.endsWith("@g.us")) return false; // 暂不接入群聊
  const text = extractText(msg);
  const media = await downloadWaMedia(entry, msg);
  if (skipEmpty && !text && !media.media_ref) return false;
  const ts = Number(msg.messageTimestamp || 0) || Math.floor(Date.now() / 1000);
  await postIngest({
    platform: "whatsapp",
    account_id: entry.accountId || "",
    chat_key: jid.split("@")[0],
    name: msg.pushName || jid.split("@")[0],
    text,
    ts,
    msg_id: (msg.key && msg.key.id) || "",
    direction: "in",
    media_type: media.media_type || "",
    media_ref: media.media_ref || "",
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
        await pushWaMessage(entry, msg, false);
      }
    } catch (e) {
      logger.debug({ e }, "messages.upsert handler failed");
    }
  });

  // 首连历史回填：messaging-history.set 携带初次同步的会话/消息
  if (WA_BACKFILL > 0) {
    sock.ev.on("messaging-history.set", async (h) => {
      try {
        const msgs = ((h && h.messages) || []).slice(-WA_BACKFILL);
        for (const msg of msgs) {
          await pushWaMessage(entry, msg, true); // 跳过无文本，降噪
        }
      } catch (e) {
        logger.debug({ e }, "messaging-history.set handler failed");
      }
    });
  }

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
  try {
    const sent = await entry.sock.sendMessage(jid, { text });
    res.json({ ok: true, message_id: (sent && sent.key && sent.key.id) || "" });
  } catch (e) {
    logger.error({ e }, "send failed");
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
