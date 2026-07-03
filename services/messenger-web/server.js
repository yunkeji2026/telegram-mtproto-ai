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
  let m = s.match(/^消息由(.+?)发送于([\s\S]+)$/);
  if (m) {
    const sender = m[1].trim();
    const rest = m[2];
    const idx = rest.indexOf("：");
    const ts = (idx >= 0 ? rest.slice(0, idx) : rest).trim();
    const text = (idx >= 0 ? rest.slice(idx + 1) : "").trim();
    return { sender, direction: sender === "你" ? "out" : "in", ts, text };
  }
  // 英文尽力兼容：Message sent by <sender> at <time>[: <text>]（时间内部冒号无空格，
  // 正文分隔为「: 」冒号+空格 → 用首个「: 」切分）。
  m = s.match(/^Message sent by (.+?) at (.+?):\s([\s\S]*)$/i);
  if (m) {
    const sender = m[1].trim();
    return { sender, direction: /^you$/i.test(sender) ? "out" : "in",
      ts: (m[2] || "").trim(), text: (m[3] || "").trim() };
  }
  m = s.match(/^Message sent by (.+?) at (.+)$/i); // 无正文（媒体）
  if (m) {
    const sender = m[1].trim();
    return { sender, direction: /^you$/i.test(sender) ? "out" : "in",
      ts: (m[2] || "").trim(), text: "" };
  }
  return null;
}

// 打开线程读末尾若干条消息（权威）。用专用 readPage（与发送用的 entry.page 隔离，避免互相打断）。
// 返回按 DOM 顺序的 [{sender,direction,ts,text}]（已过滤无正文的媒体/系统项）。
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
    await page.waitForTimeout(1000);
    const labels = await page.evaluate(() => {
      const region = document.querySelector('[role="log"]')
        || document.querySelector('[aria-label*="消息"],[aria-label*="Messages"],[aria-label*="对话"]')
        || document.querySelector('[role="main"]') || document.body;
      const MSG_ARIA = /(消息由.*发送于|Message sent by)/i;
      return Array.from(region.querySelectorAll('[aria-label]'))
        .map((e) => e.getAttribute("aria-label") || "")
        .filter((a) => MSG_ARIA.test(a))
        .slice(-15);
    });
    const msgs = [];
    for (const a of labels) {
      const p = parseMsgAria(a);
      if (p && p.text) msgs.push(p); // 只留有正文的消息（媒体/系统项无正文→跳过）
    }
    return msgs; // 数组（可能为空=已读到但无文本消息）
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

/** 读「消息请求」文件夹（陌生人首次来讯），抓两个子 tab：
 *   ①「可能认识」(FB 已滤掉明显垃圾) → category=general（允许人设自动回）；
 *   ②「垃圾信息」                    → category=spam   （只进收件箱、不自动回）。
 *  用专用页签常驻，不打扰主收件箱页。best-effort、绝不抛。 */
async function readRequests(reqPage) {
  try {
    // 每轮都重新 goto（比 SPA reload 更可靠地拉到最新未读）；等请求链接出现或超时兜底。
    await reqPage.goto(REQUESTS_URL, { waitUntil: "domcontentloaded", timeout: 15000 });
    await reqPage.waitForSelector(
      'a[href*="/requests/t/"], a[href*="/e2ee/requests/t/"]',
      { timeout: 4000 }).catch(() => {});
    await reqPage.waitForTimeout(1200);
    // ① 默认视图＝「可能认识」
    const mayKnow = await scrapeRequestRows(reqPage);
    // ② 切「垃圾信息」子 tab 抓 spam（就地换列表）；抓完切回，避免详情态残留
    let spam = [];
    if (MSG_SPAM_REQUESTS && (await clickRequestTab(reqPage, ["垃圾信息", "Spam"]))) {
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
    return out;
  } catch (e) {
    logger.debug({ e }, "readRequests failed");
    return [];
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
    // 消息请求（陌生人首次来讯，不在主列表）：专用页签常驻 /requests/，每 ~5 轮拉一次降载。
    let requests = [];
    entry._reqTick = (entry._reqTick || 0) + 1;
    if (MSG_REQUESTS && (firstPass || entry._reqTick % 5 === 0)) {
      try {
        if (!entry.reqPage || entry.reqPage.isClosed()) {
          entry.reqPage = await entry.context.newPage();
        }
        requests = await readRequests(entry.reqPage);
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
        if (!last || last.direction !== "in" || !last.text) continue;
        const inSig = `${normPreview(last.text)}|${last.ts}`;
        if (entry.lastInboundSig.get(c.key) === inSig) continue; // 该对端消息已上报过
        entry.lastInboundSig.set(c.key, inSig);
        await postIngest({
          platform: "messenger",
          account_id: entry.accountId,
          chat_key: c.key,
          name: last.sender || c.name || "", // 真实发送者（修复把 "在线" 当昵称）
          avatar_url: c.avatar || "",
          text: last.text, // 未截断全文
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
      let text = preview, name = c.name || "";
      if (MSG_READ_THREAD && MSG_READ_REQUESTS) {
        if (reqOpened >= MSG_MAX_OPENS) continue; // 超额：不推进 seen，下次请求扫描重试
        reqOpened++;
        const tail = await readThreadTail(entry, c.key);
        if (tail === null) continue; // 读失败→不推进 seen，下轮重试
        const last = tail.length ? tail[tail.length - 1] : null;
        if (last && last.direction === "in" && last.text) {
          text = last.text; name = last.sender || name; // 未截断全文 + 真实发送者
        }
        // 读到空（E2EE 不可读）→ text 保留 preview 回落，交由下方占位判定
      }
      entry.seen.set(c.key, sig);
      if (!text || E2EE_PLACEHOLDER_RE.test(text)) continue; // 空/加密横幅非真消息 → 不入库
      const inSig = normPreview(text);
      if (entry.lastInboundSig.get(c.key) === inSig) continue; // 该请求正文已上报过
      entry.lastInboundSig.set(c.key, inSig);
      await postIngest({
        platform: "messenger",
        account_id: entry.accountId,
        chat_key: c.key,
        name, // 真实发送者（进线程读到时更准）
        avatar_url: c.avatar || "",
        text, // 读到→未截断全文；读不到→列表预览
        ts: Math.floor(Date.now() / 1000),
        msg_id: "",
        direction: "in",
        is_request: true, // 陌生人首次来讯（消息请求）→ 供前端识别新客户/待接受
        request_category: cat,
      });
    }
    if (firstPass) entry._baselined = true;
    // 周期性刷新 cookie 快照（xs 会轮换；~每 15 轮≈60s 存一次，保持可 restore）。
    entry._pollCount = (entry._pollCount || 0) + 1;
    if (entry._loginId && entry._pollCount % 15 === 0) {
      await saveCookies(entry._loginId, entry.context);
    }
  } catch (e) {
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
    try {
      const prof = await readSelfProfile(entry.page);
      entry.name = prof.name;
      entry.avatarUrl = prof.avatarUrl;
    } catch (_) {}
    logger.info({ loginId, accountId: entry.accountId }, "Messenger connected");
    entry._loginId = loginId;
    await saveCookies(loginId, entry.context); // 落盘会话级 cookie，扛住重启
    startPolling(entry);
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
function scheduleRecovery(loginId) {
  if (_shuttingDown || _recovering.has(loginId)) return;
  _recovering.add(loginId);
  const old = sessions.get(loginId);
  if (old) { stopPolling(old); sessions.delete(loginId); }
  logger.warn({ loginId }, "browser context closed unexpectedly → auto-recover in 3s");
  setTimeout(async () => {
    try {
      if (_shuttingDown) return;
      await startLogin(loginId);
      logger.info({ loginId }, "context auto-recovered (relaunched from persisted profile)");
    } catch (e) {
      logger.error({ e, loginId }, "context auto-recovery failed");
    } finally {
      _recovering.delete(loginId);
    }
  }, 3000);
}

async function startLogin(loginId, proxyUrl) {
  const userDataDir = path.join(SESSIONS_DIR, loginId);
  const context = await chromium.launchPersistentContext(
    userDataDir, launchOptions(proxyUrl));
  // 崩溃自愈：context 意外关闭 → 自动重启（正常退出由 _shuttingDown 拦掉）。
  context.on("close", () => scheduleRecovery(loginId));
  await applyStealth(context);
  // restore：导航前把上次登录的会话级 cookie（xs/c_user）灌回去，免重扫。
  await loadCookies(loginId, context);
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

  try {
    await page.goto(MESSENGER_URL, { waitUntil: "domcontentloaded", timeout: 30000 });
  } catch (e) {
    logger.debug({ e }, "initial goto failed");
  }

  // 后台盯登录状态：登录成功 → 记 accountId、采身份、开轮询。
  // 窗口 30 分钟（交互登录含扫码/2FA/找窗口耗时，10 分钟偏短易误判 expired）；
  // 即便超时置 expired，/status 轮询仍会 promoteIfLoggedIn 复检补救，不会「过期即死」。
  (async () => {
    const deadline = Date.now() + 1000 * 60 * 30; // 最多盯 30 分钟
    while (sessions.has(loginId) && entry.status !== "authorized" && Date.now() < deadline) {
      try {
        if (await promoteIfLoggedIn(loginId, entry)) break;
        entry.qrImage = await snapshot(page);
      } catch (e) {
        logger.debug({ e }, "login watch tick failed");
      }
      await new Promise((r) => setTimeout(r, 1500));
    }
    if (entry.status !== "authorized" && sessions.has(loginId)) {
      entry.status = "expired";
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
      await startLogin(loginId);
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
    const rows = await readRequests(entry.reqPage);
    res.json({ requests: rows });
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
  res.json({ readOk: tail !== null, count: Array.isArray(tail) ? tail.length : 0, lastIn, tail });
});

app.post("/accounts/restore", async (_req, res) => {
  const restored = await restoreAll();
  res.json({ ok: true, restored });
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
    if (e.status === "authorized") accounts.push({ login_id: id, account_id: e.accountId });
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
    await box.click();
    await box.type(text, { delay: 20 });
    await page.keyboard.press("Enter");
    await page.waitForTimeout(1000);
    // 记录自发文本（在 verify 之前、按下 Enter 之后即记）——即使 verify 偶发误判，也确保轮询
    // 能识别并跳过这条自发消息的回声，杜绝「自己回自己」。
    recordSent(entry, jid, text);
    // 校验：发送后输入框应清空（未清空多半没发出去）。
    const sent = await verifyComposerCleared(page);
    res.json({ ok: true, message_id: "", accepted, sent });
  } catch (e) {
    logger.error({ e }, "send failed");
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

app.listen(PORT, async () => {
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
