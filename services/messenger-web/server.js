/**
 * Messenger 网页模式（web mode）登录 + 收发微服务。
 *
 * 竞品「支持一堆 app」的真相：内嵌隔离浏览器加载平台**官方网页版**，一号一隔离 profile，
 * 网页里登录（扫码/账密/2FA 都在官方页完成），再用 DOM/网络层读消息、填输入框点发送。
 * 本服务即 Messenger 的这条路——用 Playwright 驱动一个持久化 Chromium 上下文加载
 * https://www.messenger.com/ ，功能对齐官方网页版；与 Python 主进程
 * （src/integrations/messenger_web_login.py）通过本地 HTTP 桥接，契约对齐 whatsapp-baileys。
 *
 *   POST /login/start            -> { login_id, qr_image, status }   发起一次登录（弹出登录页）
 *   GET  /login/:id/status       -> { status, account_id, qr_image } 轮询登录状态（qr_image=登录页截图）
 *   POST /login/:id/cancel       -> { ok }                           取消/关闭该登录上下文
 *   POST /accounts/restore       -> { ok, restored }                 恢复磁盘已持久化的登录
 *   GET  /accounts               -> { accounts: [...] }              已登录账号
 *   POST /accounts/:id/send      -> { ok, message_id }               发消息（DOM 自动化）
 *   POST /accounts/:id/logout    -> { ok, account_id }               登出并清 profile 目录
 *   GET  /health                 -> { ok: true }
 *
 * status 取值：pending | scanned | authorized | expired | failed（与 baileys 对齐，Python 侧统一归一）
 * 每个账号用独立的持久化 userDataDir（sessions/<login_id>/），cookie 持久化 → 免重复登录。
 *
 * 运行：
 *   cd services/messenger-web && npm install && PORT=8791 node server.js
 *   （npm install 会经 postinstall 自动 playwright install chromium）
 *
 * 登录交互：默认 headed（MSG_HEADLESS=0）——弹出真实浏览器窗口，运营在本机窗口内完成
 * 官方登录（扫码 / 账密 / 2FA 都可），Playwright 只负责「持久化 + 检测登录成功 + 收发」。
 * 登录成功后可切 headless 常驻（重启后 restore 用 MSG_HEADLESS=1 后台保活）。
 *
 * 注意：网页自动化依赖 messenger.com 的 DOM 结构，平台改版可能需要微调选择器
 * （见 SEL_* 常量集中区）。存在 ToS / 风控风险，请配套一号一指纹一代理 + 养号。
 */

import express from "express";
import pino from "pino";
import { fileURLToPath } from "url";
import path from "path";
import fs from "fs";
import { chromium } from "playwright";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SESSIONS_DIR = process.env.MSG_SESSIONS_DIR || path.join(__dirname, "sessions");
const PORT = Number(process.env.PORT || 8791);
const logger = pino({ level: process.env.LOG_LEVEL || "info" });

// 登录时是否隐藏浏览器窗口。默认 headed（"0"）：运营需在弹窗里完成官方登录。
// 登录成功、profile 持久化后，可用 MSG_HEADLESS=1 后台常驻 restore。
const HEADLESS = String(process.env.MSG_HEADLESS ?? "0") === "1";
// 入站轮询间隔（毫秒）；0 关闭入站同步（仅登录+发送）。
const POLL_MS = Number(process.env.MSG_POLL_MS || 4000);
// 首次登录后回填最近 N 个会话的末条（降噪；0 关闭）。
const MSG_BACKFILL = Number(process.env.MSG_BACKFILL || 20);
// 是否轮询「消息请求」文件夹（陌生人首次来讯落这里；默认开）。
const MSG_REQUESTS = String(process.env.MSG_REQUESTS ?? "1") !== "0";
// 「进线程读正文」权威模式（默认开）：列表预览仅当变更探测器；一旦某会话有变更，进线程按
// 每条消息的无障碍标签（消息由<发送者>发送于<时间>：<正文>）读到**方向权威 + 全文 + 真实发送者**，
// 据此判定「最后一条是否对端新消息」再上报。彻底解决①预览截断②E2EE 预览不可读③方向误判自回复。
// 关闭（=0）则回落旧的「列表预览直报」（带前缀/自回声/状态名护栏）。
const MSG_READ_THREAD = String(process.env.MSG_READ_THREAD ?? "1") !== "0";
// 每轮最多进线程读取的会话数（限流，避免大量导航像 bot / 触发风控）。
const MSG_MAX_OPENS = Number(process.env.MSG_MAX_OPENS || 5);
// 消息请求（陌生人首讯）是否也进线程读全文（默认开）。真号联调确认「打开≠接受」——仅导航读取
// 不会接受/移出请求箱；读到全文 → 人设化首复更准；读不到（E2EE 不可读）→ 回落列表预览逻辑
// （占位=加密横幅非真消息，仍按旧行为跳过，不入库脏数据）。
const MSG_READ_REQUESTS = String(process.env.MSG_READ_REQUESTS ?? "1") !== "0";
// 「消息请求」文件夹降载 + 风控退避 -------------------------------------------------
// 每 N 个基础 tick 才拉一次请求文件夹（此前硬编码 5≈20s；默认调大到 15≈60s，降低对 /requests/
// 的访问频率——FB 会把高频访问该功能判为「过度使用此功能」并临时封禁）。
const MSG_REQ_EVERY = Math.max(1, Number(process.env.MSG_REQ_EVERY || 15));
// 每轮最多进线程读取的「请求」会话数（独立于主收件箱 MSG_MAX_OPENS，默认更低——请求区更敏感）。
// 置 0 → 不进请求线程读全文，仅用列表预览（不等于关闭请求同步，仍会入库预览）。
const MSG_MAX_REQ_OPENS = Math.max(0, Number(process.env.MSG_MAX_REQ_OPENS || 2));
// 检测到 /requests/ 被临时封禁（「你暂时被禁止使用此功能」/ "temporarily blocked"）时的退避冷却：
// 基础时长（默认 30min），连续命中每次翻倍直到上限（默认 6h）；冷却窗口内完全不导航 /requests/，
// 冷却结束自动恢复。
const MSG_REQ_BLOCK_COOLDOWN_MS = Math.max(0, Number(process.env.MSG_REQ_BLOCK_COOLDOWN_MS || 30 * 60 * 1000));
const MSG_REQ_BLOCK_MAX_MS = Math.max(
  MSG_REQ_BLOCK_COOLDOWN_MS,
  Number(process.env.MSG_REQ_BLOCK_MAX_MS || 6 * 60 * 60 * 1000)
);
// 入站媒体落地目录（对齐 whatsapp-baileys 的 WA_MEDIA_DIR 模式）：进线程读到媒体气泡时，
// 用浏览器会话下载并写入 Python 静态目录（同机共享），前端按 /static URL 加载。未配置则不下载
// 媒体（回落占位文本 [图片]/[视频]…，行为退回纯文本）。默认指向 messenger 静态子目录。
const MSG_MEDIA_DIR = process.env.MSG_MEDIA_DIR || "";
const MSG_MEDIA_URL_BASE = (
  process.env.MSG_MEDIA_URL_BASE || "/static/protocol_media/messenger"
).replace(/\/+$/, "");

fs.mkdirSync(SESSIONS_DIR, { recursive: true });

// Python 主进程统一收件箱入站桥（可选；未配置则不上报）。
const PY_INGEST_URL = process.env.PY_INGEST_URL || "";
const PY_API_TOKEN = process.env.PY_API_TOKEN || "";
const MSG_SYNC = String(process.env.MSG_SYNC ?? "1") !== "0";

const MESSENGER_URL = process.env.MSG_BASE_URL || "https://www.messenger.com/";

/** login_id -> { context, page, status, qrImage, accountId, name, avatarUrl,
 *               createdAt, userDataDir, proxyUrl, seen(Map thread->lastMsgId), pollTimer } */
const sessions = new Map();

function newLoginId() {
  return "msg_" + Math.random().toString(36).slice(2, 10);
}

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

// 联调用：最近入站检测环形缓冲（不依赖主程序即可核验轮询是否抓到新消息）。
const RECENT_INBOUND = [];
function recordInbound(payload) {
  RECENT_INBOUND.push({ ...payload, detected_at: new Date().toISOString() });
  while (RECENT_INBOUND.length > 40) RECENT_INBOUND.shift();
}

async function postIngest(payload) {
  recordInbound(payload);
  await postJson(PY_INGEST_URL, payload);
}

// 会话健康事件上报（P0-2 闭环）：登录/掉线/放弃自愈等关键转移主动 push 给 Python，
// 不再只靠 Python 侧轮询 /accounts 才后知后觉。URL 默认由 PY_INGEST_URL 推导
// （…/ingest → …/session-status），也可用 PY_STATUS_URL 显式覆盖。best-effort 不抛。
const PY_STATUS_URL = process.env.PY_STATUS_URL
  || (PY_INGEST_URL ? PY_INGEST_URL.replace(/\/ingest\s*$/, "/session-status") : "");
async function postStatus(loginId, entry, status, detail) {
  if (!PY_STATUS_URL) return;
  // 掉线/待重登时 entry.accountId 常为空（尚未晋级）→ 尽力从 c_user cookie 补
  // （登出后 c_user 通常残留），让 Python 侧不健康登记与后续恢复对得上同一账号。
  let acct = String((entry && entry.accountId) || "");
  if (!acct && entry && entry.context) {
    try { acct = await readAccountId(entry.context); } catch (_) { acct = ""; }
  }
  await postJson(PY_STATUS_URL, {
    platform: "messenger",
    account_id: acct,
    login_id: String(loginId || ""),
    status: String(status || ""),
    detail: String(detail || ""),
    ts: Math.floor(Date.now() / 1000),
  });
}

// ── DOM 选择器集中区（messenger.com 改版时改这里；已按真号联调校准）─────────────
// 会话列表左栏：普通会话是 a[href^="/t/"]，端到端加密会话是 a[href^="/e2ee/t/"]。
// 二者都要抓（真号实测 E2EE 占多数），线程 id 统一取 /t/<数字> 段。
const SEL_CONV_LINKS = 'a[href^="/t/"], a[href^="/e2ee/t/"]';
// 打开某会话后的消息气泡容器（Messenger 用 role=row 承载每条消息）。
const SEL_MSG_ROWS = 'div[role="row"]';
// 输入框（contenteditable 富文本）。
const SEL_COMPOSER = 'div[role="textbox"][contenteditable="true"]';

// 会话预览里代表「本方发出/系统占位」的前缀 → 入站轮询应跳过（非对端来信）。
// "你:"/"你发送了…" = 自己发的；E2EE 占位 = 无正文可读。
const OUTBOUND_PREVIEW_RE = /^(你[:：]|你发送了|你撤回了|你回复了|You sent|You:|You unsent|You replied)/;
const E2EE_PLACEHOLDER_RE = /端到端加密|end-to-end encrypt|无法显示消息|无法显示此消息|can't display/i;
// 「在线/活跃/正在输入」等**状态行**，绝非消息正文。E2EE 会话列表行常把状态/联系人名当成
// 「预览」抓出（无可读正文）→ 若不拦，会把状态或人名当消息去自动回复（如把对方名字当消息，
// 回一句"这听起来像全名"）。用于：① 选预览时跳过 ② 名字位若命中状态词说明整行被误抓→弃用该行。
const STATUS_LINE_RE = /^(在线|活跃|刚刚活跃|在线状态|正在输入.*|对方正在输入.*|active(\s+now)?|online|typing.*)$/i;

// ── 自回声抑制（根治「自己回自己的消息」死循环）───────────────────────────────
// 轮询只能从「列表预览」推断新消息，没有 msg_id/方向/时间戳。本方一发出回复，该回复就成为
// 该会话的最新预览 → 下一轮被误判为「新入站」→ 再次自动回复 → 死循环。前缀正则(你：/You:)不
// 够稳（截断/改版/请求线程格式差异会漏判）。故这里**显式记住本服务刚发出的文本**，轮询时凡
// 与近期自发文本吻合的预览一律跳过——方向判定不再依赖脆弱前缀，确定性消除自回复。
const SENT_ECHO_TTL_MS = 10 * 60 * 1000; // 自发文本的抑制窗口（10 分钟）

// 归一化预览/自发文本：剥离「你：/You:/你发送了/You sent」方向前缀 + 折叠空白 + 去尾部省略号
// （预览常被 messenger 截断）+ 小写。用于自回声/重复的鲁棒比对。
function normPreview(s) {
  return String(s || "")
    .replace(/^(你[:：]\s*|You[:：]\s*|你发送了[:：]?\s*|You sent[:：]?\s*)/i, "")
    .replace(/[\u2026…]+$/, "")
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase();
}

// 记录本服务发出的消息（chat_key + 归一化文本 + 时间），有界 + 过期清理。
function recordSent(entry, key, text) {
  if (!entry || !key || !text) return;
  if (!entry.sentLog) entry.sentLog = [];
  entry.sentLog.push({ key: String(key), norm: normPreview(text), ts: Date.now() });
  const cutoff = Date.now() - SENT_ECHO_TTL_MS;
  entry.sentLog = entry.sentLog.filter((e) => e.ts >= cutoff).slice(-100);
}

// 某预览是否吻合近期本方发出的消息（截断/前缀无关：取前 24 字，任一为另一前缀即判吻合）。
function isSelfEcho(entry, key, preview) {
  if (!entry || !entry.sentLog || !entry.sentLog.length) return false;
  const np = normPreview(preview);
  if (!np) return false;
  const cutoff = Date.now() - SENT_ECHO_TTL_MS;
  const a = np.slice(0, 24);
  for (const e of entry.sentLog) {
    if (e.ts < cutoff || e.key !== String(key)) continue;
    const b = e.norm.slice(0, 24);
    if (a && b && (e.norm.startsWith(a) || np.startsWith(b))) return true;
  }
  return false;
}

// ── 进线程读正文：解析每条消息的无障碍标签 → 权威方向 + 发送者 + 全文 ─────────────
// Messenger 每条消息是 div[role="button"]，aria-label 形如：
//   「Enter，消息由<发送者>发送于<时间>：<正文>」（中文 UI）
//   「Enter, Message sent by <sender> at <time>: <text>」（英文 UI，尽力兼容）
// 发送者为「你/You」→ 本方(out)，否则对端(in)。正文为**未截断全文**；对 E2EE 会话同样可读
// （对话正文渲染在主 frame 的 role=log 区，而非 fbsbx 沙箱 iframe）。
function parseMsgAria(aria) {
  if (!aria) return null;
  const s = String(aria).replace(/^\s*Enter\s*[，,]\s*/i, "").trim();
  // 中文：消息由<sender>发送于<time>[：<text>]。注意时间内部用半角冒号(14:44)，正文分隔用
  // **全角冒号「：」** → 用第一个全角冒号切分 时间/正文（不能用半角冒号，否则会切碎 14:44）。
  // sender 用 (.*?) 允许**为空**：E2EE 私聊里对端消息常渲染成「消息由发送于<time>：<text>」
  // （发送者名缺失）——旧的 (.+?) 会整条匹配失败 → 静默丢弃所有对端消息！空发送者视为对端(in)。
  let m = s.match(/^消息由(.*?)发送于([\s\S]+)$/);
  if (m) {
    const sender = m[1].trim();
    const rest = m[2];
    const idx = rest.indexOf("：");
    const ts = (idx >= 0 ? rest.slice(0, idx) : rest).trim();
    const text = (idx >= 0 ? rest.slice(idx + 1) : "").trim();
    return { sender, direction: sender === "你" ? "out" : "in", ts, text };
  }
  // 英文尽力兼容：Message sent by <sender> at <time>[: <text>]（sender 同样允许空）。
  m = s.match(/^Message sent by (.*?) at (.+?):\s([\s\S]*)$/i);
  if (m) {
    const sender = m[1].trim();
    return { sender, direction: /^you$/i.test(sender) ? "out" : "in",
      ts: (m[2] || "").trim(), text: (m[3] || "").trim() };
  }
  m = s.match(/^Message sent by (.*?) at (.+)$/i); // 无正文（媒体）
  if (m) {
    const sender = m[1].trim();
    return { sender, direction: /^you$/i.test(sender) ? "out" : "in",
      ts: (m[2] || "").trim(), text: "" };
  }
  return null;
}

// 用浏览器会话下载线程里的媒体元素到 MSG_MEDIA_DIR，返回 {media_type, media_ref} 或 {}。
// scontent CDN 直链用 page.request（带会话 cookie）；blob: 用页内 fetch→base64 回传。
// 失败/未配置 MSG_MEDIA_DIR → {}（上层回落占位文本，绝不阻断入站）。
async function downloadThreadMedia(page, media) {
  if (!media || !media.src || !MSG_MEDIA_DIR) return {};
  try {
    let buf = null;
    if (media.src.startsWith("data:")) {
      // E2EE 解密后内联的真图：data:[mime][;base64],<payload> → 直接解码，无需网络。
      const comma = media.src.indexOf(",");
      if (comma > 0) {
        const meta = media.src.slice(0, comma);
        const payload = media.src.slice(comma + 1);
        buf = /;base64/i.test(meta)
          ? Buffer.from(payload, "base64")
          : Buffer.from(decodeURIComponent(payload));
      }
    } else if (media.src.startsWith("blob:")) {
      const b64 = await page.evaluate(async (u) => {
        try {
          const r = await fetch(u);
          const ab = await r.arrayBuffer();
          let s = ""; const bytes = new Uint8Array(ab);
          for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
          return btoa(s);
        } catch (_) { return ""; }
      }, media.src);
      if (b64) buf = Buffer.from(b64, "base64");
    } else {
      const resp = await page.request.get(media.src, { timeout: 15000 }).catch(() => null);
      if (resp && resp.ok()) buf = Buffer.from(await resp.body());
    }
    if (!buf || !buf.length) return {};
    // 优先按 data: 的 MIME 定扩展名（图片可能是 png/webp/gif），否则按媒体类型缺省。
    let ext = media.kind === "image" ? ".jpg"
      : media.kind === "video" ? ".mp4"
      : media.kind === "voice" ? ".mp4" : ".bin";
    if (media.src.startsWith("data:")) {
      const mm = media.src.slice(0, 40).match(/^data:image\/(png|webp|gif|jpeg|jpg)/i);
      if (mm) ext = "." + mm[1].toLowerCase().replace("jpeg", "jpg");
    }
    const fname = `msg_${Date.now()}_${Math.random().toString(36).slice(2, 8)}${ext}`;
    fs.mkdirSync(MSG_MEDIA_DIR, { recursive: true });
    fs.writeFileSync(path.join(MSG_MEDIA_DIR, fname), buf);
    return { media_type: media.kind, media_ref: `${MSG_MEDIA_URL_BASE}/${fname}` };
  } catch (e) {
    logger.debug({ e }, "downloadThreadMedia failed");
    return {};
  }
}

// 媒体占位文本（下载失败/未配置目录时回落，至少让 AI/坐席知道「客户发了媒体」，对齐 Telegram）。
const MEDIA_PLACEHOLDER = { image: "[图片]", video: "[视频]", voice: "[语音]", file: "[文件]" };

// 等线程出现「真实消息」（非加密横幅）再读，防 fresh 导航只抓到「…受端到端加密保护…」占位。
// 命中一条非横幅的 MSG_ARIA 即返回 true；超时 false（上层可换 /e2ee/t 路径重试或按现状返回）。
async function waitThreadContent(page, timeoutMs = 3000) {
  try {
    await page.waitForFunction(() => {
      const region = document.querySelector('[role="log"]') || document.body;
      return Array.from(region.querySelectorAll("[aria-label]"))
        .map((e) => e.getAttribute("aria-label") || "")
        .filter((a) => /(消息由.*发送于|Message sent by)/i.test(a))
        .some((a) => !/端到端加密|end-to-end encrypt|无法显示|can't display/i.test(a));
    }, { timeout: timeoutMs });
    return true;
  } catch (_) { return false; }
}

// 打开线程读末尾若干条消息（权威）。用专用 readPage（与发送用的 entry.page 隔离，避免互相打断）。
// 返回按 DOM 顺序的 [{sender,direction,ts,text,media_type,media_ref}]（含媒体气泡：下载成功带
// media_ref，失败回落占位文本；纯系统项仍过滤）。
async function readThreadTail(entry, key) {
  if (!entry || !entry.context) return [];
  try {
    if (!entry.readPage || entry.readPage.isClosed()) {
      entry.readPage = await entry.context.newPage();
    }
    const page = entry.readPage;
    await page.goto(`${MESSENGER_URL}t/${key}`, { waitUntil: "domcontentloaded", timeout: 20000 });
    // 等消息区就绪（role=log 出现）；未出现视为渲染未就绪 → 返回 null 让上层下轮重试（不误判为空）。
    const logReady = await page.waitForSelector('[role="log"]', { timeout: 4500 })
      .then(() => true).catch(() => false);
    if (!logReady) return null;
    // 等真实消息渲染（非加密横幅）。E2EE 线程走 /t/ 有时只渲染横幅 → 换 /e2ee/t/ 再试一次。
    let hasContent = await waitThreadContent(page, 3000);
    if (!hasContent) {
      await page.goto(`${MESSENGER_URL}e2ee/t/${key}`, { waitUntil: "domcontentloaded", timeout: 20000 })
        .catch(() => {});
      await page.waitForSelector('[role="log"]', { timeout: 4500 }).catch(() => {});
      hasContent = await waitThreadContent(page, 3000);
    }
    await page.waitForTimeout(hasContent ? 400 : 800);
    // 滚到底 + 等图片解码：E2EE 图片经客户端解密后才以 blob: 挂载，初始是 data: 模糊占位；不等则
    // 只看到 data: 占位（被跳过）→ 整条媒体消息漏读。滚到底触发懒加载/解密，再等"渲染够大且非
    // data:"的图片出现（无大图=纯文本线程，立即通过不空等）。
    try {
      await page.evaluate(() => {
        const r = document.querySelector('[role="log"]') || document.scrollingElement || document.body;
        if (r) r.scrollTop = r.scrollHeight;
      });
    } catch (_) {}
    await page.waitForFunction(() => {
      const region = document.querySelector('[role="log"]') || document.body;
      const big = Array.from(region.querySelectorAll("img")).filter((im) => {
        const rc = im.getBoundingClientRect();
        return rc.width >= 128 && rc.height >= 128;
      });
      if (!big.length) return true; // 无大图 → 纯文本线程，不空等
      // 大图已解码(自然尺寸就绪)即可读；E2EE 图解密后自然尺寸变大(无论 data:/blob:/scontent)。
      return big.some((im) => (im.naturalWidth || 0) >= 128);
    }, { timeout: 3500 }).catch(() => {});
    // 每条消息取 aria-label + 其气泡容器内的媒体源（图片/视频/音频）。保守取媒体：仅当元素够大
    // （避免头像/emoji/链接预览缩略图误判为客户媒体）；有文本的气泡不强行贴图（防误贴头像）。
    const items = await page.evaluate(() => {
      const region = document.querySelector('[role="log"]')
        || document.querySelector('[aria-label*="消息"],[aria-label*="Messages"],[aria-label*="对话"]')
        || document.querySelector('[role="main"]') || document.body;
      const MSG_ARIA = /(消息由.*发送于|Message sent by)/i;
      const nodes = Array.from(region.querySelectorAll('[aria-label]'))
        .filter((e) => MSG_ARIA.test(e.getAttribute("aria-label") || ""))
        .slice(-15);
      return nodes.map((e) => {
        const aria = e.getAttribute("aria-label") || "";
        // 上溯到消息行容器（role=row 或最多 4 层父级）再找媒体
        let box = e;
        for (let i = 0; i < 4 && box.parentElement; i++) {
          if (box.getAttribute && box.getAttribute("role") === "row") break;
          box = box.parentElement;
        }
        let media = null;
        const vid = box.querySelector("video");
        const aud = box.querySelector("audio");
        const vSrc = vid && (vid.currentSrc || vid.src
          || (vid.querySelector("source") && vid.querySelector("source").src));
        if (vSrc) {
          media = { kind: "video", src: vSrc };
        } else if (aud && (aud.currentSrc || aud.src)) {
          media = { kind: "voice", src: aud.currentSrc || aud.src };
        } else {
          // 图片：容器内"渲染够大"的一张（rect 尺寸为主，兼容自然尺寸尚未 load 完）；跳过 data:
          // 占位与头像/emoji/小图标。E2EE 图解密后为 blob:/scontent，非 data: 才算真图。
          let best = null, bestArea = 0;
          for (const img of box.querySelectorAll("img")) {
            const src = img.currentSrc || img.src || "";
            if (!src) continue;
            const natW = img.naturalWidth || 0, natH = img.naturalHeight || 0;
            // data: 既可能是模糊占位(自然尺寸很小)，也可能是 E2EE 客户端解密后内联的真图(自然尺寸大，
            // 实测 natW=1080)。仅按"自然尺寸<128"滤掉占位；真图保留。非 data: 一律进尺寸门。
            if (src.startsWith("data:") && natW < 128) continue;
            const rc = img.getBoundingClientRect();
            const w = Math.max(rc.width, natW, img.width || 0);
            const h = Math.max(rc.height, natH, img.height || 0);
            const area = w * h;
            if (w >= 128 && h >= 128 && area > bestArea) { best = src; bestArea = area; }
          }
          // 背景图瓦片兜底（部分图片用 div background-image 渲染，非 <img>）
          if (!best) {
            for (const el of box.querySelectorAll("div,span,a")) {
              const rc = el.getBoundingClientRect();
              if (rc.width < 128 || rc.height < 128) continue;
              const bg = getComputedStyle(el).backgroundImage || "";
              const mm = bg.match(/url\(["']?((?:https?:|blob:)[^"')]+)["']?\)/);
              if (mm) { best = mm[1]; break; }
            }
          }
          if (best) media = { kind: "image", src: best };
        }
        return { aria, media };
      });
    });
    const msgs = [];
    for (const it of items) {
      const p = parseMsgAria(it.aria);
      if (!p) continue;
      p.media_type = ""; p.media_ref = "";
      // 有媒体：文本气泡只在「无正文」时贴媒体（防把带头像的文本消息误标成图片）
      if (it.media && (!p.text || it.media.kind !== "image")) {
        const dl = await downloadThreadMedia(page, it.media);
        if (dl.media_ref) {
          p.media_type = dl.media_type; p.media_ref = dl.media_ref;
          if (!p.text) p.text = ""; // 媒体可无正文
        } else if (!p.text) {
          p.text = MEDIA_PLACEHOLDER[it.media.kind] || "[媒体]"; // 下载失败→占位
        }
      }
      if (p.text || p.media_ref) msgs.push(p); // 有正文或媒体才留（纯系统项跳过）
    }
    return msgs; // 数组（可能为空=已读到但无文本/媒体消息）
  } catch (e) {
    logger.debug({ e, key }, "readThreadTail failed");
    return null; // 硬失败 → 上层不推进 seen，下轮重试
  }
}

/** 从 href（/t/123456）取线程 key。 */
function threadKeyFromHref(href) {
  const s = String(href || "");
  const m = s.match(/\/t\/([^/?#]+)/);
  return m ? m[1] : "";
}

/** 读取 c_user cookie（Facebook 数字账号 id）作为 account_id。 */
async function readAccountId(context) {
  try {
    const cookies = await context.cookies();
    const c = cookies.find((x) => x.name === "c_user");
    return (c && String(c.value)) || "";
  } catch (_) {
    return "";
  }
}

/** 是否已登录：须同时有 c_user（账号 id）+ xs（会话鉴权密钥）。
 *  只看 c_user 会误报——c_user 常在登出后残留，真正代表已鉴权会话的是 xs。 */
async function isLoggedIn(context) {
  try {
    const cookies = await context.cookies();
    const hasUser = cookies.some((x) => x.name === "c_user" && x.value);
    const hasAuth = cookies.some((x) => x.name === "xs" && x.value);
    return hasUser && hasAuth;
  } catch (_) {
    return false;
  }
}

/** 截当前登录页截图作为「二维码/登录面板」回传前端（data URI）。 */
async function snapshot(page) {
  try {
    const buf = await page.screenshot({ type: "png" });
    return "data:image/png;base64," + buf.toString("base64");
  } catch (_) {
    return "";
  }
}

/** best-effort 读取账号自身昵称/头像（供 self_profile 富集）。 */
async function readSelfProfile(page) {
  const out = { name: "", avatarUrl: "" };
  try {
    // 账号菜单里的头像 img 常带 alt=昵称、src=头像 URL；best-effort，失败留空。
    const info = await page.evaluate(() => {
      const img = document.querySelector('image, img[alt]');
      return {
        name: (img && img.getAttribute("alt")) || "",
        avatarUrl: (img && (img.getAttribute("src") || img.getAttribute("xlink:href"))) || "",
      };
    });
    if (info) {
      out.name = String(info.name || "");
      out.avatarUrl = String(info.avatarUrl || "");
    }
  } catch (_) {}
  return out;
}

// 真实 Chrome UA（避免暴露 HeadlessChrome / 老旧 Playwright 版本特征）。
const REAL_UA = process.env.MSG_UA ||
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36";
const LOCALE = process.env.MSG_LOCALE || "zh-CN";
const TZ = process.env.MSG_TZ || "Asia/Shanghai";

/** 浏览器启动参数（一号一代理 + 反自动化检测）。
 *  Facebook/Meta 会检测自动化浏览器（navigator.webdriver、AutomationControlled、
 *  HeadlessChrome UA 等），命中即登录后一导航就作废会话弹回登录页 → 必须 stealth。 */
function launchOptions(proxyUrl) {
  const opts = {
    headless: HEADLESS,
    userAgent: REAL_UA,
    locale: LOCALE,
    timezoneId: TZ,
    viewport: { width: 1280, height: 800 },
    args: [
      "--disable-blink-features=AutomationControlled",
      "--disable-features=IsolateOrigins,site-per-process",
      "--no-default-browser-check",
      "--no-first-run",
    ],
    ignoreDefaultArgs: ["--enable-automation"],
  };
  if (proxyUrl) opts.proxy = { server: proxyUrl };
  return opts;
}

/** 登录会话 cookie 快照路径（与 profile 目录并列）。 */
function cookiesPath(loginId) {
  return path.join(SESSIONS_DIR, `${loginId}.cookies.json`);
}

/** 保存全部 cookie（含会话级）到 JSON——持久化上下文不落盘会话级 cookie（xs/c_user），
 *  必须自存自灌。会话级（expires<0）统一顶到 +1 年，重新注入时即成持久 cookie。 */
async function saveCookies(loginId, context) {
  try {
    const cookies = await context.cookies();
    if (!cookies || !cookies.length) return;
    const oneYear = Math.floor(Date.now() / 1000) + 365 * 24 * 3600;
    const norm = cookies.map((c) => ({
      ...c,
      expires: (!c.expires || c.expires < 0) ? oneYear : c.expires,
    }));
    fs.writeFileSync(cookiesPath(loginId), JSON.stringify(norm), "utf8");
  } catch (e) {
    logger.debug({ e, loginId }, "saveCookies failed");
  }
}

/** restore 时把快照 cookie 重新注入上下文（导航前调用）。 */
async function loadCookies(loginId, context) {
  try {
    const p = cookiesPath(loginId);
    if (!fs.existsSync(p)) return false;
    const cookies = JSON.parse(fs.readFileSync(p, "utf8"));
    if (Array.isArray(cookies) && cookies.length) {
      await context.addCookies(cookies);
      return true;
    }
  } catch (e) {
    logger.debug({ e, loginId }, "loadCookies failed");
  }
  return false;
}

/** 给上下文注入反检测脚本（每页文档创建前执行）。 */
async function applyStealth(context) {
  try {
    await context.addInitScript(() => {
      // 抹掉 webdriver 标识
      Object.defineProperty(navigator, "webdriver", { get: () => undefined });
      // 伪造 plugins / languages（无头/自动化常为空 → 明显特征）
      try {
        Object.defineProperty(navigator, "languages", { get: () => ["zh-CN", "zh", "en"] });
        Object.defineProperty(navigator, "plugins", { get: () => [1, 2, 3, 4, 5] });
      } catch (_) {}
      // chrome 运行时对象（真实 Chrome 有 window.chrome）
      if (!window.chrome) window.chrome = { runtime: {} };
      // 权限查询伪装
      try {
        const orig = window.navigator.permissions && window.navigator.permissions.query;
        if (orig) {
          window.navigator.permissions.query = (p) =>
            p && p.name === "notifications"
              ? Promise.resolve({ state: Notification.permission })
              : orig(p);
        }
      } catch (_) {}
    });
  } catch (e) {
    logger.debug({ e }, "applyStealth failed");
  }
}

/** 非破坏性读会话列表：直接抓左栏，取 {线程 key, 昵称, 末条预览, 头像 URL}。
 *  messenger.com 是双栏 SPA——左栏列表在打开任意会话时都常驻，故**无需导航**即可读；
 *  仅当当前不在 messenger.com 域内时才 goto 一次。绝不逐会话点进（避免标记已读/打断运营）。 */
async function readConversations(page) {
  try {
    const url = page.url() || "";
    if (!/messenger\.com/.test(url)) {
      await page.goto(MESSENGER_URL, { waitUntil: "domcontentloaded", timeout: 15000 });
      await page.waitForTimeout(1500);
    }
    const rows = await page.$$eval(SEL_CONV_LINKS, (els) => {
      const seen = new Set();
      const out = [];
      for (const a of els) {
        const href = a.getAttribute("href") || "";
        // 只认数字线程 id（/t/123 或 /e2ee/t/123），排除 /marketplace/t、/requests/t、/archived/t
        const m = href.match(/\/t\/(\d+)/);
        if (!m) continue;
        const key = m[1];
        if (seen.has(key)) continue;
        // 爬到承载整行的 gridcell 容器
        let row = a;
        for (let i = 0; i < 6 && row.parentElement; i++) {
          row = row.parentElement;
          if (row.getAttribute && row.getAttribute("role") === "gridcell") break;
        }
        const text = ((row.innerText || a.innerText || "") + "").trim();
        if (!text) continue; // 跳过空文本（当前打开会话的头部锚点）
        const lines = text.split("\n").map((s) => s.trim()).filter(Boolean);
        const name = lines[0] || "";
        // 预览 = 名字之后第一行「非分隔符/非相对时间/非行动标签」
        let preview = "";
        for (let i = 1; i < lines.length; i++) {
          const l = lines[i];
          if (l === "·" || l === "回复？" || l === "是否跟进？" ||
              /^(在线|活跃|刚刚活跃|在线状态|正在输入|对方正在输入|active(\s+now)?|online|typing)/i.test(l) ||
              /^\d+\s*(分钟?|小时|天|周|月|年|min|h|d|w|mo|y)/.test(l)) continue;
          preview = l;
          break;
        }
        // 头像抓取（冗余兜底，防单一选择器随 messenger 改版失配 → 头像整片丢）：
        //   ① 优先 FB CDN（fbcdn/scontent）真头像；② 退而取行内任意 http(s) 图；③ 都无→空。
        const avatar = (function () {
          const imgs = row.querySelectorAll("img");
          for (const im of imgs) {
            const s = im.getAttribute("src") || "";
            if (/fbcdn|scontent/i.test(s)) return s;
          }
          for (const im of imgs) {
            const s = im.getAttribute("src") || "";
            if (/^https?:/i.test(s)) return s;
          }
          return "";
        })();
        seen.add(key);
        out.push({ key, name, preview, avatar });
      }
      return out.slice(0, 200);
    });
    return rows || [];
  } catch (e) {
    logger.debug({ e }, "readConversations failed");
    return [];
  }
}

// 请求文件夹地址（陌生人首次来讯落这里，不在主列表）。
const REQUESTS_URL = MESSENGER_URL.replace(/\/$/, "") + "/requests/";
// 预览里「未读消息：」「Unread message:」等前缀 → 清洗掉只留正文。
const PREVIEW_PREFIX_RE = /^(未读消息[:：]\s*|Unread message[:：]?\s*)/;
// 是否抓「垃圾信息」子 tab（FB 判为垃圾的陌生请求）。默认开；置 0 只抓「可能认识」。
const MSG_SPAM_REQUESTS = String(process.env.MSG_SPAM_REQUESTS ?? "1") !== "0";
// 每 N 次「请求文件夹轮询」才顺带扫一次「垃圾信息」子 tab（此前每轮都切 tab 往返；切 tab 也是一次
// 操作，拉稀到每 N 次即可进一步降载）。默认 4；置 0 或关闭 MSG_SPAM_REQUESTS 则不扫 spam。
const MSG_SPAM_EVERY = Math.max(0, Number(process.env.MSG_SPAM_EVERY ?? 4));

/** 抓当前「消息请求」视图（左栏列表）里的请求行。纯 DOM 提取，绝不抛。
 *  返回 [{key, name, preview, avatar}]。 */
async function scrapeRequestRows(page) {
  try {
    return await page.$$eval(
      'a[href*="/requests/t/"], a[href*="/e2ee/requests/t/"]', (els) => {
        const seen = new Set();
        const out = [];
        for (const a of els) {
          const href = a.getAttribute("href") || "";
          const m = href.match(/\/t\/(\d+)/);
          if (!m) continue;
          const key = m[1];
          if (seen.has(key)) continue;
          let row = a;
          for (let i = 0; i < 6 && row.parentElement; i++) {
            row = row.parentElement;
            if (row.getAttribute && row.getAttribute("role") === "gridcell") break;
          }
          const text = ((row.innerText || a.innerText || "") + "").trim();
          if (!text) continue;
          const lines = text.split("\n").map((s) => s.trim()).filter(Boolean);
          const name = lines[0] || "";
          let preview = "";
          for (let i = 1; i < lines.length; i++) {
            const l = lines[i];
            if (l === "·" || l === "未读消息：" || l === "未读消息:" ||
                /^\d+\s*(分钟?|小时|天|周|月|年|min|h|d|w|mo|y)/.test(l)) continue;
            preview = l;
            break;
          }
          const img = row.querySelector('img[src*="fbcdn"]');
          const avatar = img ? (img.getAttribute("src") || "") : "";
          seen.add(key);
          out.push({ key, name, preview, avatar });
        }
        return out.slice(0, 100);
      });
  } catch (_) {
    return [];
  }
}

/** 切到指定 tab（Messenger 请求页用 role=tab 的 SPA 子视图，就地换列表、不改 URL）。
 *  names 依次尝试（中/英），命中即点，返回是否点到。绝不抛。 */
async function clickRequestTab(page, names) {
  for (const name of names) {
    try {
      const loc = page.getByRole("tab", { name });
      if (await loc.count()) { await loc.first().click({ timeout: 3000 }); return true; }
    } catch (_) {}
  }
  return false;
}

/** 探测 /requests/ 是否被 FB 临时风控封禁（扳手错误页：「你暂时被禁止使用此功能 / 似乎你过度
 *  使用了此功能」/ English "You're temporarily blocked … misusing this feature by going too fast"）。
 *  命中 → 调用方进入退避冷却，停止再导航/切 tab/点开，避免续命甚至加重封禁。绝不抛。
 *  为降误报：仅当命中文案「且」页面无任何真实请求线程链接时才判封禁（预览恰好含关键词不误伤）。 */
const REQUESTS_BLOCK_RE = /暂时被禁止使用此功能|过度使用了此功能|暂时被阻止|temporarily blocked|misusing this feature|going too fast/i;
async function isRequestsBlocked(page) {
  try {
    return await page.evaluate((reSrc) => {
      const re = new RegExp(reSrc, "i");
      const txt = (document.body && document.body.innerText) || "";
      if (!re.test(txt)) return false;
      const hasRows = document.querySelector(
        'a[href*="/requests/t/"], a[href*="/e2ee/requests/t/"]');
      return !hasRows;
    }, REQUESTS_BLOCK_RE.source);
  } catch (_) {
    return false;
  }
}

/** 读「消息请求」文件夹（陌生人首次来讯），抓两个子 tab：
 *   ①「可能认识」(FB 已滤掉明显垃圾) → category=general（允许人设自动回）；
 *   ②「垃圾信息」                    → category=spam   （只进收件箱、不自动回）。
 *  用专用页签常驻，不打扰主收件箱页。best-effort、绝不抛。 */
async function readRequests(reqPage, entry = null) {
  try {
    // 每轮都重新 goto（比 SPA reload 更可靠地拉到最新未读）；等请求链接出现或超时兜底。
    await reqPage.goto(REQUESTS_URL, { waitUntil: "domcontentloaded", timeout: 15000 });
    await reqPage.waitForSelector(
      'a[href*="/requests/t/"], a[href*="/e2ee/requests/t/"]',
      { timeout: 4000 }).catch(() => {});
    // 风控封禁探测（放在等待之后，确保 SPA 已渲染错误页文案）：命中 → 立刻返回 blocked，
    // 绝不再切 tab / 等待 / 点开，交由调用方进入退避冷却。
    if (await isRequestsBlocked(reqPage)) {
      return { blocked: true, rows: [] };
    }
    await reqPage.waitForTimeout(1200);
    // ① 默认视图＝「可能认识」
    const mayKnow = await scrapeRequestRows(reqPage);
    // ② 切「垃圾信息」子 tab 抓 spam（就地换列表）；抓完切回，避免详情态残留。
    //    降载：不再每轮都切 tab，改为首轮 + 每 MSG_SPAM_EVERY 次请求轮询才扫一次 spam。
    let spam = [];
    const pollCount = entry ? (entry._reqPollCount = (entry._reqPollCount || 0) + 1) : 1;
    const doSpam = MSG_SPAM_REQUESTS && MSG_SPAM_EVERY > 0 &&
      (pollCount === 1 || pollCount % MSG_SPAM_EVERY === 0);
    if (doSpam && (await clickRequestTab(reqPage, ["垃圾信息", "Spam"]))) {
      await reqPage.waitForTimeout(1500);
      spam = await scrapeRequestRows(reqPage);
      await clickRequestTab(reqPage, ["可能认识", "You may know"]);
    }
    const spamKeys = new Set(spam.map((r) => r.key));
    const norm = (r, category) => ({
      ...r,
      preview: (r.preview || "").replace(PREVIEW_PREFIX_RE, "").trim(),
      isRequest: true,
      category,
    });
    const out = [];
    // spam key 优先归 spam（防同一线程在两 tab 都出现时误判为可自动回）
    for (const r of mayKnow) { if (!spamKeys.has(r.key)) out.push(norm(r, "general")); }
    for (const r of spam) out.push(norm(r, "spam"));
    return { blocked: false, rows: out };
  } catch (e) {
    logger.debug({ e }, "readRequests failed");
    return { blocked: false, rows: [] };
  }
}

/** 入站轮询：抓左栏列表预览，把「新出现的对端来信」push 进 Python。best-effort、绝不抛。
 *  非破坏性——只读列表，不点进会话（不改已读态、不打断运营手动操作）。
 *  首轮只建立 seen 基线不上报（避免把历史会话末条全当新消息灌进来）。 */
async function pollInbound(entry) {
  if (!MSG_SYNC || !PY_INGEST_URL || !entry.accountId || entry.status !== "authorized") return;
  if (entry._polling) return;
  entry._polling = true;
  try {
    const firstPass = !entry._baselined;
    // 主收件箱列表（常规会话）
    const convs = await readConversations(entry.page);
    // 消息请求（陌生人首次来讯，不在主列表）：专用页签常驻 /requests/，每 MSG_REQ_EVERY 轮拉一次降载。
    // 命中 FB 风控封禁 → 进入退避冷却（指数增长）；冷却窗口内完全不导航 /requests/，冷却结束自动恢复。
    let requests = [];
    entry._reqTick = (entry._reqTick || 0) + 1;
    const reqDue = MSG_REQUESTS && (firstPass || entry._reqTick % MSG_REQ_EVERY === 0);
    const inCooldown = entry._reqBlockedUntil && Date.now() < entry._reqBlockedUntil;
    if (reqDue && inCooldown) {
      logger.debug(
        { accountId: entry.accountId, until: entry._reqBlockedUntil },
        "requests poll skipped (block cooldown)");
    } else if (reqDue) {
      try {
        if (!entry.reqPage || entry.reqPage.isClosed()) {
          entry.reqPage = await entry.context.newPage();
        }
        const res = await readRequests(entry.reqPage, entry);
        if (res.blocked) {
          entry._reqBlockStreak = (entry._reqBlockStreak || 0) + 1;
          const cd = Math.min(
            MSG_REQ_BLOCK_MAX_MS,
            MSG_REQ_BLOCK_COOLDOWN_MS * Math.pow(2, entry._reqBlockStreak - 1));
          entry._reqBlockedUntil = Date.now() + cd;
          logger.warn(
            { accountId: entry.accountId, cooldownMs: cd, streak: entry._reqBlockStreak },
            "messenger requests folder temporarily blocked by FB; backing off");
        } else {
          requests = res.rows;
          if (entry._reqBlockStreak) {
            logger.info(
              { accountId: entry.accountId },
              "messenger requests folder recovered; block cooldown cleared");
          }
          entry._reqBlockStreak = 0;
          entry._reqBlockedUntil = 0;
        }
      } catch (e) { logger.debug({ e }, "requests poll failed"); }
    }
    const all = convs.concat(requests);
    // 头像直链缓存：供 GET /accounts/:id/avatar 按需返回（不额外导航/点进会话）。每轮用当前
    // 列表整体刷新 → 天然有界 且直链保持新鲜（scontent token 有时效，Python 取后立即
    // 下载落 /static 稳定托管）。
    const ac = new Map();
    for (const c of all) { if (c.avatar) ac.set(c.key, c.avatar); }
    entry.avatarCache = ac;
    if (!entry.lastInboundSig) entry.lastInboundSig = new Map();

    // ── 主列表会话：变更探测 → 权威「进线程读正文」───────────────────────────────
    const candidates = [];
    for (const c of convs) {
      const preview = (c.preview || "").trim();
      if (!preview) continue;
      const sig = `${c.key}:${preview.slice(0, 160)}`;
      if (entry.seen.get(c.key) === sig) continue; // 预览无变化 → 无新活动
      if (firstPass) { entry.seen.set(c.key, sig); continue; } // 首轮仅建基线
      const _nm = (c.name || "").trim();
      // 便宜预筛（不必进线程即可判定「非对端新消息」）：本方近期自发（自回声）、明显 "你:" 出站、
      // 状态行/名字误抓噪声 → 直接推进 seen 跳过，避免无谓导航（也降风控/减少标已读）。
      if (isSelfEcho(entry, c.key, preview)
          || OUTBOUND_PREVIEW_RE.test(preview)
          || STATUS_LINE_RE.test(_nm) || STATUS_LINE_RE.test(preview)
          || (normPreview(preview) === normPreview(_nm) && normPreview(_nm))) {
        entry.seen.set(c.key, sig);
        continue;
      }
      // 有变更且疑似对端 → 候选（含 E2EE 占位预览：列表读不到，但进线程能读到正文）。
      candidates.push({ c, sig });
    }

    if (!firstPass && MSG_READ_THREAD) {
      // 进线程按无障碍标签读**方向权威 + 全文 + 真实发送者**，据「最后一条对端消息」上报；
      // 本方出站永不推进 lastInboundSig → 从根上杜绝自回复。每轮限流 MSG_MAX_OPENS。
      let opened = 0;
      for (const { c, sig } of candidates) {
        if (opened >= MSG_MAX_OPENS) break; // 超额留待下轮（seen 未推进→自然重试）
        opened++;
        const tail = await readThreadTail(entry, c.key);
        if (tail === null) continue; // 渲染未就绪/失败 → 不推进 seen，下轮重试
        entry.seen.set(c.key, sig); // 已成功读取（含空）→ 推进变更基线
        // 只在**整段会话的最后一条**是对端消息时才回：若最后一条是本方发出（我们已回过），
        // 绝不回；这也确定性杜绝了「回自己」——本方出站永远不会成为待回的 last。
        const last = tail.length ? tail[tail.length - 1] : null;
        // 最后一条须是对端消息，且有正文**或媒体**（媒体气泡可无正文）。
        if (!last || last.direction !== "in" || (!last.text && !last.media_ref)) continue;
        // 加密横幅（"…受端到端加密保护…"）非真消息 → 不入库（无媒体时）。
        if (last.text && !last.media_ref && E2EE_PLACEHOLDER_RE.test(last.text)) continue;
        const inSig = `${normPreview(last.text)}|${last.media_ref || ""}|${last.ts}`;
        if (entry.lastInboundSig.get(c.key) === inSig) continue; // 该对端消息已上报过
        entry.lastInboundSig.set(c.key, inSig);
        await postIngest({
          platform: "messenger",
          account_id: entry.accountId,
          chat_key: c.key,
          name: last.sender || c.name || "", // 真实发送者（修复把 "在线" 当昵称）
          avatar_url: c.avatar || "",
          text: last.text, // 未截断全文（媒体可为空）
          media_type: last.media_type || "",
          media_ref: last.media_ref || "",
          ts: Math.floor(Date.now() / 1000),
          msg_id: "",
          direction: "in",
          is_request: false,
          request_category: "",
        });
      }
    } else if (!firstPass) {
      // 回落：MSG_READ_THREAD=0 → 旧的「列表预览直报」（前面预筛已挡掉出站/自回声/噪声）。
      for (const { c, sig } of candidates) {
        entry.seen.set(c.key, sig);
        if (E2EE_PLACEHOLDER_RE.test(c.preview || "")) continue; // 占位不可读→不报
        await postIngest({
          platform: "messenger", account_id: entry.accountId, chat_key: c.key,
          name: c.name || "", avatar_url: c.avatar || "", text: (c.preview || "").trim(),
          ts: Math.floor(Date.now() / 1000), msg_id: "", direction: "in",
          is_request: false, request_category: "",
        });
      }
    }

    // ── 消息请求（陌生人首次来讯）：变更探测 → 权威进线程读全文（保 category）+ 读不到回落预览 ──
    // 「打开≠接受」已真号联调确认（仅导航读取不接受/不移出请求箱）。读到全文 → 人设化首复更准；
    // 读到空/仍是 E2EE 加密横幅 → 按旧行为不入库（横幅非真消息）。限流独立 reqOpened。
    let reqOpened = 0;
    for (const c of requests) {
      const preview = (c.preview || "").trim();
      if (!preview) continue;
      const sig = `${c.key}:${preview.slice(0, 160)}`;
      if (entry.seen.get(c.key) === sig) continue;
      if (firstPass) { entry.seen.set(c.key, sig); continue; }
      const _nm = (c.name || "").trim();
      // 噪声护栏（请求本是入站，出站/自回声一般不命中，保留防御）→ 推进 seen 跳过
      if (OUTBOUND_PREVIEW_RE.test(preview) || isSelfEcho(entry, c.key, preview)
          || STATUS_LINE_RE.test(_nm) || STATUS_LINE_RE.test(preview)
          || (normPreview(preview) === normPreview(_nm) && normPreview(_nm))) {
        entry.seen.set(c.key, sig);
        continue;
      }
      const cat = c.category || "general"; // general 可自动回 / spam 仅入收件箱
      let text = preview, name = c.name || "", mType = "", mRef = "";
      if (MSG_READ_THREAD && MSG_READ_REQUESTS && MSG_MAX_REQ_OPENS > 0) {
        if (reqOpened >= MSG_MAX_REQ_OPENS) continue; // 超额：不推进 seen，下次请求扫描重试
        reqOpened++;
        const tail = await readThreadTail(entry, c.key);
        if (tail === null) continue; // 读失败→不推进 seen，下轮重试
        const last = tail.length ? tail[tail.length - 1] : null;
        if (last && last.direction === "in" && (last.text || last.media_ref)) {
          text = last.text; name = last.sender || name; // 未截断全文 + 真实发送者
          mType = last.media_type || ""; mRef = last.media_ref || ""; // 首讯即媒体
        }
        // 读到空（E2EE 不可读）→ text 保留 preview 回落，交由下方占位判定
      }
      entry.seen.set(c.key, sig);
      // 无正文且无媒体，或仍是加密横幅 → 非真消息，不入库
      if ((!text && !mRef) || (text && E2EE_PLACEHOLDER_RE.test(text))) continue;
      const inSig = `${normPreview(text)}|${mRef}`;
      if (entry.lastInboundSig.get(c.key) === inSig) continue; // 该请求正文已上报过
      entry.lastInboundSig.set(c.key, inSig);
      await postIngest({
        platform: "messenger",
        account_id: entry.accountId,
        chat_key: c.key,
        name, // 真实发送者（进线程读到时更准）
        avatar_url: c.avatar || "",
        text, // 读到→未截断全文；读不到→列表预览
        media_type: mType,
        media_ref: mRef,
        ts: Math.floor(Date.now() / 1000),
        msg_id: "",
        direction: "in",
        is_request: true, // 陌生人首次来讯（消息请求）→ 供前端识别新客户/待接受
        request_category: cat,
      });
    }
    if (firstPass) entry._baselined = true;
    // 高频刷新 cookie 快照（xs 会轮换）：每 2 轮≈8s 存一次。此前 60s 一次 → 崩溃/被强杀时快照
    // 常滞后于已轮换的 xs，自愈 restore 灌回过期 xs 即登出。8s 窗口内 xs 几乎不会已失效。
    entry._pollCount = (entry._pollCount || 0) + 1;
    if (entry._loginId && entry._pollCount % 2 === 0) {
      await saveCookies(entry._loginId, entry.context);
    }
    // 轮询健康信号：登录态确认（cookie 未失效）+ 时间戳。用于 Python 侧 healthy() 判活，
    // 修「cookie 失效但列表仍显示 authorized」的假健康（此前 /accounts 只看 status 字段）。
    // 每 ~15 轮(≈60s)做一次 pageLoggedIn 复检，避免每轮都跑 DOM 查询。
    try {
      if ((entry._pollCount || 0) % 15 === 0) {
        entry._loggedIn = await pageLoggedIn(entry.page);
      }
    } catch (_) { /* 复检失败不改判定，保守留旧值 */ }
    entry._lastPollOkTs = Date.now();
    entry._lastPollErr = "";
  } catch (e) {
    entry._lastPollErr = String((e && e.message) || e || "poll failed");
    entry._lastPollErrTs = Date.now();
    logger.debug({ e }, "pollInbound failed");
  } finally {
    entry._polling = false;
  }
}

function startPolling(entry) {
  if (!POLL_MS || entry.pollTimer) return;
  entry.pollTimer = setInterval(() => { pollInbound(entry).catch(() => {}); }, POLL_MS);
}

function stopPolling(entry) {
  if (entry && entry.pollTimer) {
    clearInterval(entry.pollTimer);
    entry.pollTimer = null;
  }
}

/** 页面级登录校验：真正进了收件箱（无登录表单、且有收件箱骨架）才算数。
 *  单看 cookie 会误报——c_user/xs 可能残留或失效，服务端仍渲染登录页。 */
async function pageLoggedIn(page) {
  try {
    return await page.evaluate(() => {
      // 有邮箱/密码输入框 → 还在登录页 → 未登录
      const loginForm = document.querySelector(
        'input[name="pass"], input[type="password"], input[name="email"]');
      if (loginForm) return false;
      // 正向信号：会话链接 / 富文本输入框 / 网格骨架任一存在
      const inbox = document.querySelector(
        'a[href^="/t/"], div[role="textbox"][contenteditable="true"], [role="grid"]');
      return !!inbox;
    });
  } catch (_) {
    return false;
  }
}

/** 检测到已登录则把 entry 晋级为 authorized（采身份 + 开轮询），幂等。
 *  被登录看门狗与 /status 轮询共用 → 即使看门狗超时退出，一次状态轮询也能复检补救。
 *  返回是否已授权。 */
async function promoteIfLoggedIn(loginId, entry) {
  if (!entry) return false;
  if (entry.status === "authorized") return true;
  try {
    // cookie 预检（快）+ 页面级确认（准）：两者都过才判已登录，杜绝 xs 残留误报。
    if (!(await isLoggedIn(entry.context))) return false;
    if (!(await pageLoggedIn(entry.page))) return false;
    entry.accountId = await readAccountId(entry.context);
    entry.status = "authorized";
    entry._loggedIn = true;
    try {
      const prof = await readSelfProfile(entry.page);
      entry.name = prof.name;
      entry.avatarUrl = prof.avatarUrl;
    } catch (_) {}
    logger.info({ loginId, accountId: entry.accountId }, "Messenger connected");
    entry._loginId = loginId;
    _recoveryAttempts.delete(loginId); // 成功授权 → 清零自愈退避计数
    const _srt = _slowRetryTimers.get(loginId); // 已恢复 → 撤掉排队中的慢重试
    if (_srt) { clearTimeout(_srt); _slowRetryTimers.delete(loginId); }
    await saveCookies(loginId, entry.context); // 落盘会话级 cookie，扛住重启
    startPolling(entry);
    postStatus(loginId, entry, "authorized", "connected").catch(() => {});
    return true;
  } catch (e) {
    logger.debug({ e, loginId }, "promoteIfLoggedIn failed");
    return false;
  }
}

/** 拉起一个登录/账号上下文（持久化 profile）。loginId 复用即为 restore。 */
// ── 崩溃自愈 ─────────────────────────────────────────────────────────────────
// 浏览器 context 崩溃/被关（headed 长时运行 + 进线程频繁导航偶发；或被系统/FB 干掉）会让轮询
// 静默失败 → 服务「假死」：HTTP 仍在、/health ok，但读不到消息也不回。此前无自愈 → 一崩就一直
// 冻着。此处监听 context 'close' 事件：清理旧 entry/定时器 → 延时用 startLogin 重启（复用磁盘
// profile + 回灌 cookie，通常免重扫）。用 _recovering 防并发重建；_shuttingDown 时不自愈（正常退出）。
const _recovering = new Set();
// 自愈退避 + 上限：连续崩溃时用指数退避（3s→6s→12s→24s→48s，封顶 60s），超过 5 次即放弃自愈、
// 置 expired 并告警——防「崩溃循环无限 3s 重启」既堆 Chromium 又高频重连触发 FB 反自动化登出。
// 距上次尝试 >5min 视为新事件，计数清零（promoteIfLoggedIn 成功授权后亦清零）。
const _recoveryAttempts = new Map();
const _RECOVERY_MAX = 5;
// 放弃自愈后的慢速重试（P2）：快自愈（3s→60s 退避 ×5 次）放弃后不再是「死等人工」——
// 每 MSG_RECOVERY_SLOW_RETRY_MS（默认 15min，0=关）安排一次全新自愈周期（计数清零）。
// 频率足够低：不堆 Chromium、对 FB 只是一次导航（cookie 失效时停在登录页，无登录尝试），
// 但能自动救回「系统资源暂时耗尽/网络中断恢复」这类过后即愈的场景。人工重登成功会清零一切。
const MSG_RECOVERY_SLOW_RETRY_MS = Math.max(
  0, Number(process.env.MSG_RECOVERY_SLOW_RETRY_MS ?? 15 * 60 * 1000));
const _slowRetryTimers = new Map();
function scheduleSlowRetry(loginId) {
  if (!MSG_RECOVERY_SLOW_RETRY_MS || _shuttingDown) return;
  if (_slowRetryTimers.has(loginId)) return; // 已排队
  const t = setTimeout(() => {
    _slowRetryTimers.delete(loginId);
    if (_shuttingDown) return;
    const cur = sessions.get(loginId);
    if (cur && cur.status === "authorized") return; // 期间已恢复（人工登录/自行回来）
    logger.info({ loginId }, "slow-retry: starting a fresh auto-recovery cycle");
    _recoveryAttempts.delete(loginId); // 全新退避预算
    scheduleRecovery(loginId);
  }, MSG_RECOVERY_SLOW_RETRY_MS);
  if (typeof t.unref === "function") t.unref();
  _slowRetryTimers.set(loginId, t);
}
function scheduleRecovery(loginId) {
  if (_shuttingDown || _recovering.has(loginId)) return;
  const now = Date.now();
  const rec = _recoveryAttempts.get(loginId) || { count: 0, lastTs: 0 };
  if (now - rec.lastTs > 5 * 60 * 1000) rec.count = 0;
  rec.count += 1; rec.lastTs = now;
  _recoveryAttempts.set(loginId, rec);
  const old0 = sessions.get(loginId);
  if (rec.count > _RECOVERY_MAX) {
    logger.error({ loginId, attempts: rec.count },
      "context crash-loop → GIVING UP auto-recovery (manual re-login needed). "
      + "Not relaunching to avoid Chromium pile-up / anti-automation logout.");
    if (old0) { stopPolling(old0); old0.status = "expired"; }
    postStatus(loginId, old0, "expired",
      `context crash-loop (${rec.count} attempts) → auto-recovery given up; manual re-login needed`)
      .catch(() => {});
    scheduleSlowRetry(loginId); // 不死等人工：低频再给自愈机会
    return;
  }
  _recovering.add(loginId);
  if (old0) { stopPolling(old0); sessions.delete(loginId); }
  const delay = Math.min(3000 * Math.pow(2, rec.count - 1), 60000);
  logger.warn({ loginId, attempt: rec.count, delayMs: delay },
    "browser context closed unexpectedly → auto-recover");
  setTimeout(async () => {
    try {
      if (_shuttingDown) return;
      await startLogin(loginId, "", true);
      logger.info({ loginId }, "context auto-recovered (relaunched from persisted profile)");
    } catch (e) {
      logger.error({ e, loginId }, "context auto-recovery failed");
    } finally {
      _recovering.delete(loginId);
    }
  }, delay);
}

async function startLogin(loginId, proxyUrl, isRestore = false) {
  const userDataDir = path.join(SESSIONS_DIR, loginId);
  const context = await chromium.launchPersistentContext(
    userDataDir, launchOptions(proxyUrl));
  // 崩溃自愈：context 意外关闭 → 自动重启（正常退出由 _shuttingDown 拦掉）。
  context.on("close", () => scheduleRecovery(loginId));
  await applyStealth(context);
  const page = context.pages()[0] || (await context.newPage());

  const entry = {
    context, page,
    status: "pending",
    qrImage: "",
    accountId: "",
    name: "",
    avatarUrl: "",
    createdAt: Date.now(),
    userDataDir,
    proxyUrl: proxyUrl || "",
    seen: new Map(),
    pollTimer: null,
  };
  sessions.set(loginId, entry);

  // profile-first restore：先用持久化 profile 自身的 cookie 导航——它往往已含**最新** xs
  // （持久化上下文会随浏览器写盘）。仅当 profile 自身未登录时，才回落注入快照 .cookies.json 并重载。
  // 杜绝「用滞后的快照 xs 覆盖 profile 里更新的会话 → 反被打成过期登出」这条根因。
  try {
    await page.goto(MESSENGER_URL, { waitUntil: "domcontentloaded", timeout: 30000 });
  } catch (e) {
    logger.debug({ e }, "initial goto failed");
  }
  try {
    if (!(await pageLoggedIn(page)) && (await loadCookies(loginId, context))) {
      logger.info({ loginId }, "profile not logged in → injecting cookie snapshot fallback");
      await page.goto(MESSENGER_URL, { waitUntil: "domcontentloaded", timeout: 30000 }).catch(() => {});
    }
  } catch (e) {
    logger.debug({ e }, "profile-first cookie fallback failed");
  }

  // 后台盯登录状态：登录成功 → 记 accountId、采身份、开轮询。
  // 交互登录窗口 30 分钟（含扫码/2FA/找窗口）；restore/自愈无人工介入 → 90s 判不出即置 expired
  // 并**显式告警**（不再静默限 30 分钟；编排器/UI 能立刻看到「需重新登录」）。
  // 即便超时置 expired，/status 轮询仍会 promoteIfLoggedIn 复检补救（用户手动登录后自动恢复）。
  (async () => {
    // 交互登录 30min；restore/自愈给足人工重登时间 10min，但**一旦确认没自动授权即刻告警**
    // （编排器/运营即时看到「需重登」），并继续盯到超时——期间人工登录成功即自动 promote。
    const started = Date.now();
    const deadline = started + (isRestore ? 1000 * 60 * 10 : 1000 * 60 * 30);
    let warnedReLogin = false;
    while (sessions.has(loginId) && entry.status !== "authorized" && Date.now() < deadline) {
      try {
        if (await promoteIfLoggedIn(loginId, entry)) break;
        entry.qrImage = await snapshot(page);
        // 正常自愈通常 2-4s 内就 re-auth；>10s 仍停在登录页 → 判定需人工重登，告警一次（不误报）。
        if (isRestore && !warnedReLogin && Date.now() - started > 10000) {
          warnedReLogin = true;
          logger.warn({ loginId },
            "restore: not auto-authorized after 10s (cookies expired/invalidated) → "
            + "MANUAL RE-LOGIN likely required; watching 10min for manual login");
          postStatus(loginId, entry, "needs_login",
            "cookies expired/invalidated; manual re-login required").catch(() => {});
        }
      } catch (e) {
        logger.debug({ e }, "login watch tick failed");
      }
      await new Promise((r) => setTimeout(r, 1500));
    }
    if (entry.status !== "authorized" && sessions.has(loginId)) {
      entry.status = "expired";
      if (isRestore) {
        logger.warn({ loginId }, "restore/recover watch window ended without auth → status=expired");
        postStatus(loginId, entry, "expired",
          "restore watch window ended without auth (manual re-login required)").catch(() => {});
      }
    }
  })().catch((e) => logger.debug({ e }, "login watcher crashed"));

  return entry;
}

/** 恢复磁盘上已持久化的所有账号 profile（开机/主动调用，幂等）。 */
async function restoreAll() {
  let dirs = [];
  try {
    dirs = fs.readdirSync(SESSIONS_DIR, { withFileTypes: true })
      .filter((d) => d.isDirectory()).map((d) => d.name);
  } catch (_) {
    dirs = [];
  }
  let restored = 0;
  for (const loginId of dirs) {
    if (sessions.has(loginId)) continue;
    try {
      await startLogin(loginId, "", true);
      restored += 1;
    } catch (e) {
      logger.warn({ e, loginId }, "restore session failed");
    }
  }
  return restored;
}

/** 消息请求线程里点「接受」按钮（接受后才出现输入框）。找到并点击返回 true。
 *  兼容中英：接受/Accept。非请求线程无此按钮 → 返回 false（无副作用）。 */
async function clickAcceptRequest(page) {
  for (const name of ["接受", "Accept"]) {
    try {
      const loc = page.getByRole("button", { name, exact: true });
      if (await loc.count()) {
        await loc.first().click({ timeout: 3000 });
        return true;
      }
    } catch (_) {}
  }
  // 兜底：按可见文本精确匹配的 role=button
  try {
    const loc = page.locator('div[role="button"]', { hasText: /^(接受|Accept)$/ });
    if (await loc.count()) {
      await loc.first().click({ timeout: 3000 });
      return true;
    }
  } catch (_) {}
  return false;
}

/** 发送后校验：输入框应已清空（回车成功发出后 Messenger 会清空 composer）。
 *  若我们刚输入的文本仍在 → 多半没发出去。 */
async function verifyComposerCleared(page) {
  try {
    const txt = await page.$eval(SEL_COMPOSER,
      (el) => ((el.innerText || el.textContent || "") + "").trim());
    return txt.length === 0;
  } catch (_) {
    return false;
  }
}

// 消息行内/行侧的「发送失败」标记（Messenger 在失败气泡下方标注）。仅在**匹配到我们
// 刚发的那条 out 气泡的行容器内**查找 → 客户消息正文里偶然含这些词不会误伤。
const SEND_FAIL_MARKER_RE =
  /(无法发送|未能发送|发送失败|couldn[’']t send|didn[’']t send|failed to send|message failed)/i;

/** 送达二次确认（P1 升级）：composer 清空只是「大概率发出」，这里回读消息区最后几条
 *  out 气泡，确认我们的文本已渲染为本方消息、且该气泡行内没有「无法发送」失败标记。
 *  返回 {found, rowFail}：
 *    found=true, rowFail=false → 确认送达；
 *    found=true, rowFail=true  → 渲染了但被标失败（确定性失败）；
 *    found=false               → 超时未见（**不定态**，调用方须按「已发出」处理避免重发刷屏）。 */
async function readbackLastOutgoing(page, text, timeoutMs = 5000) {
  const want = normPreview(text).slice(0, 48);
  if (!want) return { found: false, rowFail: false };
  const t0 = Date.now();
  while (true) {
    try {
      const probe = await page.evaluate(() => {
        const region = document.querySelector('[role="log"]') || document.body;
        const MSG = /(消息由.*发送于|Message sent by)/i;
        const nodes = Array.from(region.querySelectorAll("[aria-label]"))
          .filter((e) => MSG.test(e.getAttribute("aria-label") || ""))
          .slice(-6);
        return nodes.map((e) => {
          // 上溯到消息行容器（role=row 或最多 4 层父级），带出行文本供失败标记检测
          let box = e;
          for (let i = 0; i < 4 && box.parentElement; i++) {
            if (box.getAttribute && box.getAttribute("role") === "row") break;
            box = box.parentElement;
          }
          const sib = box.nextElementSibling;
          return {
            aria: e.getAttribute("aria-label") || "",
            rowText: ((box.innerText || "") + " " + ((sib && sib.innerText) || "")).slice(0, 400),
          };
        });
      });
      for (const it of (probe || []).reverse()) {
        const p = parseMsgAria(it.aria);
        if (!p || p.direction !== "out") continue;
        const got = normPreview(p.text).slice(0, 48);
        if (got && (got.startsWith(want) || want.startsWith(got))) {
          return { found: true, rowFail: SEND_FAIL_MARKER_RE.test(it.rowText || "") };
        }
      }
    } catch (_) { /* 探测失败不改判定，重试到超时 */ }
    if (Date.now() - t0 >= timeoutMs) return { found: false, rowFail: false };
    await page.waitForTimeout(400);
  }
}

/** 轮询等待 composer 清空（发出成功的确定性信号）。发送后 Messenger 通常瞬间清空，
 *  但慢网/重渲染下可能滞后；轮询避免把「慢」误判成「没发出去」。返回是否已清空。 */
async function waitComposerCleared(page, timeoutMs = 3000) {
  const t0 = Date.now();
  // 首检立即做（成功路径几乎无延迟）；未清空则短间隔重试到超时。
  // eslint-disable-next-line no-constant-condition
  while (true) {
    if (await verifyComposerCleared(page)) return true;
    if (Date.now() - t0 >= timeoutMs) return false;
    await page.waitForTimeout(300);
  }
}

/** 从 composer 区选一个最合适的 <input type=file>：视觉媒体(图片/视频)优先挑 accept 含
 *  image/video 的；音频/文件挑不限制 image 的；都没命中则回落第一个。找不到返回 null。 */
async function pickFileInput(page, mediaType) {
  const infos = await page.$$eval('input[type="file"]', (els) =>
    els.map((el, i) => ({ i, accept: (el.getAttribute("accept") || "").toLowerCase() })));
  if (!infos.length) return null;
  const isVisual = /^(image|photo|video)/.test(String(mediaType || ""));
  let pick = infos.find((x) => isVisual
    ? (x.accept.includes("image") || x.accept.includes("video"))
    : (!x.accept || (!x.accept.includes("image") && !x.accept.includes("video"))));
  if (!pick) pick = infos[0];
  const handles = await page.$$('input[type="file"]');
  return handles[pick.i] || handles[0] || null;
}

/** 等附件预览出现（缩略图旁的「移除」键）再发送，避免回车发空。best-effort（超时也继续）。 */
async function waitForAttachmentPreview(page, timeoutMs = 8000) {
  const removeSel = [
    '[aria-label="移除"]', '[aria-label="删除"]', '[aria-label="移除附件"]',
    '[aria-label="Remove"]', '[aria-label="Remove attachment"]',
  ].join(",");
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    if (await page.$(removeSel)) return true;
    await page.waitForTimeout(400);
  }
  return false;
}

/** 把本地文件挂到 Messenger 输入区并发送（图片/视频/音频/文件通吃）。
 *  找不到 file input 时先点「附加/添加照片或视频」唤出再重试。 */
async function attachAndSend(page, mediaPath, mediaType, caption) {
  let input = await pickFileInput(page, mediaType);
  if (!input) {
    for (const s of [
      '[aria-label="附加文件"]', '[aria-label="选择文件"]', '[aria-label="添加照片或视频"]',
      '[aria-label="Attach a file"]', '[aria-label="Choose a file to upload"]',
      '[aria-label="Add photos/videos"]',
    ]) {
      const b = await page.$(s);
      if (b) { await b.click().catch(() => {}); await page.waitForTimeout(600); break; }
    }
    input = await pickFileInput(page, mediaType);
  }
  if (!input) throw new Error("file input not found");
  await input.setInputFiles(mediaPath);
  await waitForAttachmentPreview(page);
  if (caption) {
    const box = await page.$(SEL_COMPOSER);
    if (box) { await box.click(); await box.type(caption, { delay: 20 }); }
  }
  await page.keyboard.press("Enter");
  await page.waitForTimeout(2500);
}

/** 按 accountId 找已授权 session。 */
function findByAccount(accountId) {
  for (const [, e] of sessions.entries()) {
    if (e.status === "authorized" && e.accountId === String(accountId)) return e;
  }
  return null;
}

const app = express();
app.use(express.json());

app.get("/health", (_req, res) => res.json({ ok: true }));

// 联调用：查看轮询最近检测到的入站消息（核验入站链路，不依赖主程序）。
app.get("/debug/inbound", (_req, res) => res.json({ recent: RECENT_INBOUND }));

// 联调用：直接跑一遍「消息请求」解析，核验陌生人首次来讯抓取。
app.get("/debug/requests", async (req, res) => {
  const id = String(req.query.id || "");
  const entry = id ? sessions.get(id) : [...sessions.values()].find((e) => e.status === "authorized");
  if (!entry || entry.status !== "authorized") return res.status(404).json({ error: "no authorized session" });
  try {
    if (!entry.reqPage || entry.reqPage.isClosed()) entry.reqPage = await entry.context.newPage();
    const rr = await readRequests(entry.reqPage, entry);
    res.json({ requests: rr.rows, blocked: rr.blocked });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

// 临时联调：只看当前页状态（不导航），判断会话是否稳定。
app.get("/debug/state", async (req, res) => {
  const id = String(req.query.id || "");
  const entry = id ? sessions.get(id) : [...sessions.values()][0];
  if (!entry) return res.status(404).json({ error: "no session" });
  try {
    // 枚举所有 frame（含 iframe），逐一找 composer / 接受按钮 → 定位 E2EE 对话区所在 frame
    const frameInfo = [];
    for (const f of entry.page.frames()) {
      try {
        const r = await f.evaluate(() => ({
          composer: document.querySelectorAll('div[role="textbox"][contenteditable="true"]').length,
          acceptTxt: Array.from(document.querySelectorAll("div,span,button"))
            .some((el) => /^(接受|Accept)$/.test(((el.innerText || el.textContent || "") + "").trim())),
        }));
        frameInfo.push({ url: (f.url() || "").slice(0, 80), name: f.name(),
          composer: r.composer, accept: r.acceptTxt });
      } catch (_) {
        frameInfo.push({ url: (f.url() || "").slice(0, 80), name: f.name(), err: true });
      }
    }
    const info = await entry.page.evaluate(() => ({
      url: location.href,
      title: document.title,
      hasLoginForm: !!document.querySelector('input[name="pass"], input[type="password"]'),
      convCount: document.querySelectorAll('a[href^="/t/"]').length,
      hasComposer: !!document.querySelector('div[role="textbox"][contenteditable="true"]'),
      webdriver: navigator.webdriver,
      // 列出所有可编辑框 → 区分「消息输入框」vs「搜索框」，用于校准 composer 选择器
      textboxes: Array.from(document.querySelectorAll(
        '[role="textbox"], [contenteditable="true"], input[type="text"], input[type="search"]'
      )).slice(0, 10).map((el) => ({
        tag: el.tagName,
        role: el.getAttribute("role"),
        aria: el.getAttribute("aria-label"),
        placeholder: el.getAttribute("placeholder"),
        editable: el.getAttribute("contenteditable"),
      })),
      // 找文本正好是「接受」的元素，回溯祖先看谁是可点击容器（校准 clickAcceptRequest）
      acceptBtns: (() => {
        const out = [];
        const all = document.querySelectorAll("div,span,a,button");
        for (const el of all) {
          const t = ((el.innerText || el.textContent || "") + "").trim();
          if (!/^(接受|Accept)$/.test(t)) continue;
          const chain = [];
          let cur = el;
          for (let i = 0; i < 4 && cur; i++) {
            chain.push({ tag: cur.tagName, role: cur.getAttribute("role"),
              aria: cur.getAttribute("aria-label"), tabindex: cur.getAttribute("tabindex") });
            cur = cur.parentElement;
          }
          out.push({ text: t, chain });
          if (out.length >= 4) break;
        }
        return out;
      })(),
      bodyHead: (document.body.innerText || "").slice(0, 200),
    }));
    info.frames = frameInfo;
    const cookies = await entry.context.cookies();
    info.cookieNames = cookies.map((c) => c.name).filter((n) =>
      ["c_user", "xs", "datr", "sb", "fr"].includes(n));
    try {
      const buf = await entry.page.screenshot({ type: "png" });
      fs.writeFileSync(path.join(__dirname, "_state_shot.png"), buf);
      info.shot = "_state_shot.png";
    } catch (_) {}
    res.json(info);
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

// ── 临时联调用：dump 真实 DOM 结构以校准选择器（self_profile / 会话列表）。────────
// 生产可删；只在已授权 session 上工作，navigate 到收件箱首页后取样。
app.get("/debug/dom", async (req, res) => {
  const id = String(req.query.id || "");
  const entry = id ? sessions.get(id) : [...sessions.values()].find((e) => e.status === "authorized");
  if (!entry || entry.status !== "authorized") {
    return res.status(404).json({ error: "no authorized session" });
  }
  try {
    const target = req.query.path ? (MESSENGER_URL.replace(/\/$/, "") + String(req.query.path)) : MESSENGER_URL;
    await entry.page.goto(target, { waitUntil: "domcontentloaded", timeout: 20000 });
    await entry.page.waitForTimeout(2500);
    const dump = await entry.page.evaluate(() => {
      const pick = (el) => el ? {
        tag: el.tagName,
        role: el.getAttribute("role"),
        aria: el.getAttribute("aria-label"),
        alt: el.getAttribute("alt"),
        src: (el.getAttribute("src") || el.getAttribute("xlink:href") || "").slice(0, 120),
        text: (el.innerText || el.textContent || "").slice(0, 80),
      } : null;
      // 身份候选：页面上所有带 alt 的 image/img + 顶栏账号菜单
      const imgs = Array.from(document.querySelectorAll("image[alt], img[alt]"))
        .slice(0, 12).map(pick);
      // 会话列表：含普通/E2EE/请求/marketplace 各类线程链接
      const convAnchors = Array.from(document.querySelectorAll(
        'a[href^="/t/"], a[href^="/e2ee/t/"], a[href*="/requests/t/"], a[href*="/marketplace/t/"]'
      )).slice(0, 12);
      const convs = convAnchors.map((a) => {
        // 尽量爬到承载整行的容器（role=row 或 li 或 grid cell）
        let row = a;
        for (let i = 0; i < 6 && row.parentElement; i++) {
          row = row.parentElement;
          if (row.getAttribute && (row.getAttribute("role") === "row" ||
              row.getAttribute("role") === "gridcell" || row.tagName === "LI")) break;
        }
        return {
          href: a.getAttribute("href"),
          aria: a.getAttribute("aria-label"),
          anchorText: (a.innerText || "").slice(0, 120),
          rowRole: row.getAttribute && row.getAttribute("role"),
          rowText: (row.innerText || "").slice(0, 200),
          imgAlt: (() => { const im = row.querySelector && row.querySelector("image[alt], img[alt]"); return im ? im.getAttribute("alt") : ""; })(),
          imgSrc: (() => { const im = row.querySelector && row.querySelector("image, img"); return im ? (im.getAttribute("src") || im.getAttribute("xlink:href") || "").slice(0, 120) : ""; })(),
        };
      });
      // 全站锚点 href 前缀直方图 + 关键 role 计数，判断会话列表究竟怎么渲染
      const hrefHist = {};
      for (const a of Array.from(document.querySelectorAll("a[href]"))) {
        const h = a.getAttribute("href") || "";
        const key = h.split("?")[0].split("/").slice(0, 3).join("/") || h.slice(0, 20);
        hrefHist[key] = (hrefHist[key] || 0) + 1;
      }
      const roleCount = {};
      for (const r of ["row", "gridcell", "grid", "listitem", "list", "navigation", "main"]) {
        roleCount[r] = document.querySelectorAll(`[role="${r}"]`).length;
      }
      const bodyText = (document.body.innerText || "").slice(0, 1500);
      return { title: document.title, url: location.href, imgs, convs, hrefHist, roleCount, bodyText };
    });
    try {
      const buf = await entry.page.screenshot({ type: "png", fullPage: false });
      fs.writeFileSync(path.join(__dirname, "_authed_shot.png"), buf);
      dump.shot = "_authed_shot.png";
    } catch (_) {}
    res.json(dump);
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

// 联调：跑一遍 readThreadTail（进线程读正文的完整路径），核验解析后的方向/全文/发送者。
app.get("/debug/tail", async (req, res) => {
  const id = String(req.query.id || "");
  const entry = id ? sessions.get(id) : [...sessions.values()].find((e) => e.status === "authorized");
  if (!entry || entry.status !== "authorized") return res.status(404).json({ error: "no authorized session" });
  const thread = String(req.query.thread || "");
  if (!thread) return res.status(400).json({ error: "thread required" });
  const tail = await readThreadTail(entry, thread);
  let lastIn = null;
  if (Array.isArray(tail)) { for (const m of tail) { if (m.direction === "in") lastIn = m; } }
  // 诊断：readPage 导航后到底渲染了什么（定位「只抓到加密横幅」的根因）。
  let probe = null;
  try {
    const page = entry.readPage;
    probe = await page.evaluate(() => {
      const all = Array.from(document.querySelectorAll("[aria-label]"))
        .map((e) => e.getAttribute("aria-label") || "");
      const msgLike = all.filter((a) => /(消息由.*发送于|Message sent by)/i.test(a));
      return {
        url: location.href,
        hasLog: !!document.querySelector('[role="log"]'),
        rowCount: document.querySelectorAll('div[role="row"]').length,
        iframeCount: document.querySelectorAll("iframe").length,
        ariaTotal: all.length,
        msgLikeCount: msgLike.length,
        msgSample: msgLike.slice(-6).map((a) => a.slice(0, 60)),
        imgCount: document.querySelectorAll("img").length,
        videoCount: document.querySelectorAll("video").length,
        // 最后一条消息行的图片候选明细（诊断媒体漏读：看 src 前缀/自然尺寸/渲染尺寸）
        lastRowImgs: (() => {
          const region2 = document.querySelector('[role="log"]') || document.body;
          const MSG = /(消息由.*发送于|Message sent by)/i;
          const nodes = Array.from(region2.querySelectorAll("[aria-label]"))
            .filter((e) => MSG.test(e.getAttribute("aria-label") || ""));
          const last = nodes[nodes.length - 1];
          if (!last) return [];
          let box = last;
          for (let i = 0; i < 4 && box.parentElement; i++) {
            if (box.getAttribute && box.getAttribute("role") === "row") break;
            box = box.parentElement;
          }
          return Array.from(box.querySelectorAll("img")).slice(0, 8).map((im) => {
            const rc = im.getBoundingClientRect();
            const src = im.currentSrc || im.src || "";
            return { src: src.slice(0, 22), natW: im.naturalWidth || 0,
              rW: Math.round(rc.width), rH: Math.round(rc.height) };
          });
        })(),
      };
    });
  } catch (e) { probe = { error: String(e) }; }
  res.json({ readOk: tail !== null, count: Array.isArray(tail) ? tail.length : 0, lastIn, tail, probe });
});

app.post("/accounts/restore", async (_req, res) => {
  const restored = await restoreAll();
  res.json({ ok: true, restored });
});

// 人工重登通道（P2 自愈闭环）：cookie 彻底失效/自愈放弃后，运营从后台一键触发——
// 复用**同一 profile 目录**重启上下文并打开 30 分钟交互登录窗口（headed 弹窗，运营在
// 窗口内完成官方账密/2FA）。不新建 loginId → 不堆积孤儿 Chromium profile，登录成功后
// account_id 不变，编排器/收件箱无需任何重绑。:id 可为 account_id 或 login_id。
app.post("/accounts/:id/relogin", async (req, res) => {
  const want = String(req.params.id || "");
  let loginId = "";
  // 先按 login_id 直查，再按 account_id 反查（不健康会话多半未 authorized，
  // findByAccount 只认 authorized → 这里全量扫）。
  if (sessions.has(want)) {
    loginId = want;
  } else {
    for (const [id, e] of sessions.entries()) {
      if (String(e.accountId || "") === want) { loginId = id; break; }
    }
  }
  // 内存无会话（崩溃后被清）→ 磁盘 profile 还在也可重登
  if (!loginId && fs.existsSync(path.join(SESSIONS_DIR, want))) loginId = want;
  if (!loginId) {
    return res.status(404).json({ ok: false, error: "no session/profile found for id" });
  }
  try {
    const old = sessions.get(loginId);
    const proxyUrl = (old && old.proxyUrl) || "";
    // 压住崩溃自愈竞态：context.close 会触发 scheduleRecovery → 用 _recovering 占位。
    _recovering.add(loginId);
    try {
      if (old) {
        stopPolling(old);
        sessions.delete(loginId);
        try { await old.context.close(); } catch (_) {}
      }
      _recoveryAttempts.delete(loginId); // 人工介入 → 自愈放弃计数清零
      const _t = _slowRetryTimers.get(loginId); // 撤掉排队中的慢重试（人工接管）
      if (_t) { clearTimeout(_t); _slowRetryTimers.delete(loginId); }
      const entry = await startLogin(loginId, proxyUrl, false); // 交互窗口 30min
      try { await entry.page.bringToFront(); } catch (_) {}
      logger.info({ loginId }, "manual relogin window opened (30min interactive watch)");
      res.json({ ok: true, login_id: loginId, status: entry.status });
    } finally {
      _recovering.delete(loginId);
    }
  } catch (e) {
    logger.error({ e, loginId }, "relogin failed");
    res.status(500).json({ ok: false, error: String(e) });
  }
});

// 优雅关闭端点（Windows 强杀不触发信号处理 → 用它先刷盘再退出，防登录丢失）。
app.post("/shutdown", async (_req, res) => {
  res.json({ ok: true });
  setTimeout(() => gracefulShutdown("http"), 200);
});

app.post("/login/start", async (req, res) => {
  try {
    const loginId = newLoginId();
    const proxyUrl = (req.body && req.body.proxy_url) || "";
    const entry = await startLogin(loginId, proxyUrl);
    // 等首帧登录页截图（最多 ~8s）
    const deadline = Date.now() + 8000;
    while (!entry.qrImage && entry.status === "pending" && Date.now() < deadline) {
      entry.qrImage = await snapshot(entry.page);
      if (entry.qrImage) break;
      await new Promise((r) => setTimeout(r, 300));
    }
    res.json({ login_id: loginId, qr_image: entry.qrImage, status: entry.status });
  } catch (e) {
    logger.error({ e }, "start failed");
    res.status(500).json({ error: String(e) });
  }
});

app.get("/login/:id/status", async (req, res) => {
  const entry = sessions.get(req.params.id);
  if (!entry) return res.json({ status: "expired", detail: "session not found" });
  // 未授权时先复检登录态（看门狗超时/慢登录的补救）；仍未登录才刷新登录页截图。
  if (entry.status !== "authorized") {
    const ok = await promoteIfLoggedIn(req.params.id, entry);
    if (!ok) {
      try { entry.qrImage = await snapshot(entry.page); } catch (_) {}
    }
  }
  res.json({
    status: entry.status,
    account_id: entry.accountId,
    name: entry.name || "",
    avatar_url: entry.avatarUrl || "",
    qr_image: entry.status === "authorized" ? "" : entry.qrImage,
  });
});

app.post("/login/:id/cancel", async (req, res) => {
  const entry = sessions.get(req.params.id);
  if (entry) {
    stopPolling(entry);
    try { await entry.context.close(); } catch (_) {}
    sessions.delete(req.params.id);
  }
  res.json({ ok: true });
});

app.get("/accounts", (_req, res) => {
  const accounts = [];
  for (const [id, e] of sessions.entries()) {
    if (e.status === "authorized") {
      accounts.push({
        login_id: id,
        account_id: e.accountId,
        // 健康细节（P0-2）：Python healthy() 据此识别「status 仍 authorized 但登录态已丢/
        // 轮询早已停摆」的假健康。logged_in 由轮询周期性 pageLoggedIn 复检维护。
        logged_in: e._loggedIn !== false,
        last_poll_ok_ts: Math.floor((e._lastPollOkTs || 0) / 1000),
        last_poll_err: String(e._lastPollErr || ""),
      });
    }
  }
  res.json({ accounts });
});

app.post("/accounts/:id/send", async (req, res) => {
  const entry = findByAccount(req.params.id);
  if (!entry || !entry.page) {
    return res.status(404).json({ ok: false, error: "account not connected" });
  }
  const jid = String((req.body && req.body.jid) || "");
  const text = String((req.body && req.body.text) || "");
  if (!jid || !text) {
    return res.status(400).json({ ok: false, error: "jid and text required" });
  }
  try {
    const page = entry.page;
    await page.goto(`${MESSENGER_URL}t/${jid}`, {
      waitUntil: "domcontentloaded", timeout: 20000,
    });
    // settle：请求线程会先乐观渲染输入框、再换成「接受」栏，须等 UI 稳定再判定。
    await page.waitForTimeout(2000);
    // 先处理「接受」——有此按钮即消息请求，须先接受才可回复（回复即接受，符合获客策略）。
    const accepted = await clickAcceptRequest(page);
    if (accepted) await page.waitForTimeout(2000);
    // 只认真正的消息输入框（aria-label「发消息给…」/ Message…），排除搜索框等。
    const box = await page.waitForSelector(SEL_COMPOSER, { timeout: 10000 }).catch(() => null);
    if (!box) {
      return res.status(500).json({ ok: false, error: "composer not found (thread may need manual accept)" });
    }
    // 记录自发文本（在真正尝试发送前即记）——即使后续 verify 偶发误判，也确保轮询能识别
    // 并跳过这条自发消息的回声，杜绝「自己回自己」。幂等：重试不重复记（recordSent 去重）。
    recordSent(entry, jid, text);
    // 一次输入+回车+校验清空的原子尝试；返回是否确认发出（composer 清空）。
    const attemptSend = async () => {
      await box.click();
      await box.type(text, { delay: 20 });
      await page.keyboard.press("Enter");
      return await waitComposerCleared(page, 3000);
    };
    let sent = await attemptSend();
    // 安全重试一次：composer 未清空＝多半没发出。重试前确认 composer 里确实还是我们要发的
    // 文本（避免「其实已发出、只是清空滞后」时重复发送造成刷屏）。仍是原文才再发一次。
    if (!sent) {
      let stillHasOurText = false;
      try {
        const cur = await page.$eval(SEL_COMPOSER,
          (el) => ((el.innerText || el.textContent || "") + "").trim());
        stillHasOurText = cur.length > 0 && text.trim().startsWith(cur.slice(0, 8));
      } catch (_) { stillHasOurText = false; }
      if (stillHasOurText) {
        logger.warn({ jid }, "send: composer not cleared → retrying once");
        sent = await attemptSend();
      }
    }
    // 关键修复：sent=false（未确认发出）必须如实上报为失败，让 Python 侧记 autosend_failed、
    // 不把「没发出去」的草稿标记为已送达（此前恒 ok:true → 静默丢消息）。
    if (!sent) {
      logger.error({ jid }, "send: composer still not cleared after retry → reporting NOT delivered");
      return res.status(502).json({
        ok: false, delivered: false, accepted, sent: false,
        error: "composer not cleared after send (message likely not delivered)",
      });
    }
    // 送达二次确认（P1）：回读消息区，确认我们的文本已渲染成本方气泡且无「无法发送」标记。
    // - 命中失败标记 → 确定性失败，如实 502（Messenger 不会自动重发失败气泡，重投不刷屏）；
    // - 回读命中且干净 → verified:true；
    // - 超时未见（DOM 改版/虚拟列表滚动等）→ **不定态按已发出处理**（composer 已清空），
    //   verified:false 仅作观测——绝不因回读不确定而触发重发（重复消息比漏发更伤客户体验）。
    const rb = await readbackLastOutgoing(page, text, 5000);
    if (rb.found && rb.rowFail) {
      logger.error({ jid }, "send: bubble rendered with FAIL marker → reporting NOT delivered");
      return res.status(502).json({
        ok: false, delivered: false, accepted, sent: false, verified: true,
        error: "messenger marked the message as failed to send",
      });
    }
    if (!rb.found) {
      logger.warn({ jid }, "send: composer cleared but readback did not find our bubble "
        + "(treating as delivered, verified=false)");
    }
    res.json({ ok: true, delivered: true, message_id: "", accepted, sent: true,
      verified: !!rb.found });
  } catch (e) {
    logger.error({ e }, "send failed");
    res.status(500).json({ ok: false, delivered: false, error: String(e) });
  }
});

// M-parity①：出站媒体（图片/视频/音频/文件）——挂本地文件到 composer 发送，使 Messenger 与
// Telegram 的 send_media 对称。Python 侧 MessengerWebWorker 有 send_media 即被编排器判为
// owns_media=True → 工作台「图片/语音/视频/文件」按钮对 Messenger 一并点亮。语音走
// media_type=voice（作为音频文件发出，Messenger 内联可播放）。media_path 为主机本地绝对路径
// （Node 与 Python 同机，直接 setInputFiles，无需上传）。
app.post("/accounts/:id/send-media", async (req, res) => {
  const entry = findByAccount(req.params.id);
  if (!entry || !entry.page) {
    return res.status(404).json({ ok: false, error: "account not connected" });
  }
  const jid = String((req.body && req.body.jid) || "");
  const mediaPath = String((req.body && req.body.media_path) || "");
  const mediaType = String((req.body && req.body.media_type) || "");
  const caption = String((req.body && req.body.caption) || "");
  if (!jid || !mediaPath) {
    return res.status(400).json({ ok: false, error: "jid and media_path required" });
  }
  if (!fs.existsSync(mediaPath)) {
    return res.status(400).json({ ok: false, error: "media_path not found on host" });
  }
  try {
    const page = entry.page;
    await page.goto(`${MESSENGER_URL}t/${jid}`, {
      waitUntil: "domcontentloaded", timeout: 20000,
    });
    await page.waitForTimeout(2000);
    const accepted = await clickAcceptRequest(page);
    if (accepted) await page.waitForTimeout(2000);
    const box = await page.waitForSelector(SEL_COMPOSER, { timeout: 10000 }).catch(() => null);
    if (!box) {
      return res.status(500).json({ ok: false, error: "composer not found (thread may need manual accept)" });
    }
    await attachAndSend(page, mediaPath, mediaType, caption);
    // 记录自发（含 caption）→ 轮询自回声抑制；无 caption 记媒体占位。
    recordSent(entry, jid, caption || "[媒体]");
    // 送达二次确认（P1，媒体版）：有 caption 才回读（按文本匹配我们的气泡 + 失败标记）；
    // 无 caption 的纯媒体无法可靠锚定「我们这条」→ 跳过校验（宁可不定态，不冒误报失败
    // 触发重发刷屏的险）。命中失败标记 → 如实 502。
    let verified = false;
    if (caption) {
      const rb = await readbackLastOutgoing(page, caption, 5000);
      if (rb.found && rb.rowFail) {
        logger.error({ jid }, "send-media: bubble rendered with FAIL marker → NOT delivered");
        return res.status(502).json({
          ok: false, delivered: false, accepted, sent: false, verified: true,
          error: "messenger marked the media message as failed to send",
        });
      }
      verified = !!rb.found;
    }
    res.json({ ok: true, message_id: "", accepted, verified });
  } catch (e) {
    logger.error({ e }, "send-media failed");
    res.status(500).json({ ok: false, error: String(e) });
  }
});

// C：会话头像直链——复用入站轮询已抓到的 scontent 缓存（entry.avatarCache），不额外导航/点进会话
// （避免打扰运营、触发风控）。Python 侧据此下载落 /static 稳定托管，规避 scontent token 时效+跨域。
// 无缓存/未开轮询/无该线程 → 空 url（Python 回落首字母头像，绝不因头像拖累会话渲染）。
app.get("/accounts/:id/avatar", (req, res) => {
  const entry = findByAccount(req.params.id);
  if (!entry) return res.status(404).json({ ok: false, error: "account not connected" });
  const thread = String(req.query.thread || "");
  if (!thread) return res.status(400).json({ ok: false, error: "thread required" });
  const url = (entry.avatarCache && entry.avatarCache.get(thread)) || "";
  res.json({ ok: true, url });
});

app.post("/accounts/:id/logout", async (req, res) => {
  const accountId = String(req.params.id);
  let loginId = "";
  let entry = null;
  for (const [id, e] of sessions.entries()) {
    if (e.accountId === accountId) { loginId = id; entry = e; break; }
  }
  if (entry) {
    stopPolling(entry);
    try { await entry.context.close(); } catch (_) {}
  }
  if (loginId) sessions.delete(loginId);
  // 清持久化 profile 目录 → 防 restoreAll 复活
  try {
    const dir = (entry && entry.userDataDir) ||
      (loginId ? path.join(SESSIONS_DIR, loginId) : "");
    if (dir && fs.existsSync(dir)) fs.rmSync(dir, { recursive: true, force: true });
  } catch (e) {
    logger.debug({ e }, "logout profile cleanup failed");
  }
  postStatus(loginId, entry, "logged_out", "logout requested via API").catch(() => {});
  res.json({ ok: true, account_id: accountId });
});

// 优雅关闭：先关所有持久化上下文（把 cookie/session 刷盘），再退出。
// 否则强杀 Chromium 会丢失最后一批未落盘 cookie → 登录状态丢失、需重扫。
let _shuttingDown = false;
async function gracefulShutdown(signal) {
  if (_shuttingDown) return;
  _shuttingDown = true;
  logger.info({ signal }, "shutting down, flushing browser contexts");
  for (const [id, e] of sessions.entries()) {
    stopPolling(e);
    if (e.status === "authorized") { try { await saveCookies(id, e.context); } catch (_) {} }
    try { await e.context.close(); } catch (_) {}
  }
  process.exit(0);
}
process.on("SIGINT", () => gracefulShutdown("SIGINT"));
process.on("SIGTERM", () => gracefulShutdown("SIGTERM"));
// Windows 下 Stop-Process 走 SIGBREAK；也挂上。
process.on("SIGBREAK", () => gracefulShutdown("SIGBREAK"));

const _server = app.listen(PORT, async () => {
  logger.info(`Messenger web login service on :${PORT} (sessions: ${SESSIONS_DIR})`);
  // 后台常驻场景（MSG_HEADLESS=1）开机恢复已登录账号。headed 交互登录一般不自动 restore。
  if (String(process.env.MSG_RESTORE_ON_BOOT ?? (HEADLESS ? "1" : "0")) === "1") {
    try {
      const restored = await restoreAll();
      logger.info(`restored ${restored} persisted Messenger session(s) on boot`);
    } catch (e) {
      logger.error({ e }, "boot restore failed");
    }
  }
});
// 单实例守卫：端口被占 = 已有一个 messenger-web 在跑。第二个实例若继续启动，会对**同一个
// 持久化 profile** 并发拉起浏览器上下文 → 互抢 userDataDir → 上下文崩溃循环 + Chromium 堆积
// + cookie 竞争登出（本次联调实测踩中两次）。直接退出，杜绝重复实例。
_server.on("error", (err) => {
  if (err && err.code === "EADDRINUSE") {
    logger.error(`port ${PORT} already in use — another messenger-web instance is running. `
      + `Exiting to avoid duplicate browser contexts on the same profile (prior crash-loop/logout root cause).`);
  } else {
    logger.error({ err }, "http server error → exiting");
  }
  process.exit(1);
});
