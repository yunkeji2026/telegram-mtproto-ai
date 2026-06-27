"use strict";

// 后端 sidecar 命令解析纯函数单测（无框架，node 直跑）：node test/backend-launcher.test.js
const assert = require("assert");
const path = require("path");
const { resolveBackendSpawn, healthUrl, webEnvFromBackend, createBackendManager } = require("../backend-launcher.js");

let pass = 0;
function ok(name, cond) {
  assert.ok(cond, name);
  pass++;
}

const APP_DIR = "/repo/desktop"; // 假定 desktop 目录；其上级=仓库根 /repo
const REPO = path.resolve(APP_DIR, "..");

// ── ① 显式关闭 → null（用户自管，零回归）────────────────────────────────────
ok(
  "spawn.enabled=false → null",
  resolveBackendSpawn({
    config: { backend: { spawn: { enabled: false } } },
    appDir: APP_DIR, platform: "win32", exists: () => true,
  }) === null
);

// ── ② 显式 command 覆写优先级最高 ────────────────────────────────────────────
const explicit = resolveBackendSpawn({
  config: { backend: { spawn: { command: "C:/py/python.exe", args: ["server.py"], cwd: "C:/app" } } },
  appDir: APP_DIR, platform: "win32", exists: () => true,
});
ok("explicit kind", explicit && explicit.kind === "explicit");
ok("explicit command", explicit.command === "C:/py/python.exe");
ok("explicit args", explicit.args.join(",") === "server.py");
ok("explicit cwd", explicit.cwd === "C:/app");

// ── ③ 发布态：随包二进制存在 → bundled ───────────────────────────────────────
const binPath = path.join("/Resources", "backend", "backend.exe");
const bundled = resolveBackendSpawn({
  config: {}, isPackaged: true, resourcesPath: "/Resources",
  appDir: APP_DIR, platform: "win32", exists: (p) => p === binPath,
});
ok("bundled kind", bundled && bundled.kind === "bundled");
ok("bundled command", bundled.command === binPath);
// 无 dataDir → cwd 回退二进制目录；仍注入桌面模式标记（打包态默认桌面）
ok("bundled 无dataDir cwd=dirname", bundled.cwd === path.dirname(binPath));
ok("bundled 桌面模式标记", bundled.env && bundled.env.AITR_DESKTOP_MODE === "1");
ok("bundled 无dataDir 不重定向数据", bundled.env && !bundled.env.AITR_DATA_DIR);

// 发布态 + dataDir → cwd/env 指向可写数据根（核心：config 落可写区）
const DATA = path.join("/Users/me/AppData", "data");
const bundledData = resolveBackendSpawn({
  config: {}, isPackaged: true, resourcesPath: "/Resources", dataDir: DATA,
  appDir: APP_DIR, platform: "win32", exists: (p) => p === binPath,
});
ok("bundled+dataDir cwd=dataDir", bundledData.cwd === DATA);
ok("bundled+dataDir AITR_DATA_DIR", bundledData.env.AITR_DATA_DIR === DATA);
ok("bundled+dataDir AITR_CONFIG_PATH", bundledData.env.AITR_CONFIG_PATH === path.join(DATA, "config", "config.yaml"));
ok("bundled+dataDir 桌面模式标记", bundledData.env.AITR_DESKTOP_MODE === "1");

// 发布态 + backend 配置 → web host/port/token 对齐桌面壳（renderer 才连得上后端）
const bundledWeb = resolveBackendSpawn({
  config: { backend: { base_url: "http://127.0.0.1:18799", token: "admin" } },
  isPackaged: true, resourcesPath: "/Resources",
  appDir: APP_DIR, platform: "win32", exists: (p) => p === binPath,
});
ok("bundled web host", bundledWeb.env.AITR_WEB_HOST === "127.0.0.1");
ok("bundled web port", bundledWeb.env.AITR_WEB_PORT === "18799");
ok("bundled web token", bundledWeb.env.AITR_WEB_TOKEN === "admin");

// webEnvFromBackend 纯函数：解析 + 容错
const we = webEnvFromBackend({ base_url: "http://localhost:9000", token: "t" });
ok("webEnv host", we.AITR_WEB_HOST === "localhost");
ok("webEnv port", we.AITR_WEB_PORT === "9000");
ok("webEnv token", we.AITR_WEB_TOKEN === "t");
ok("webEnv 非法 base_url 容错", Object.keys(webEnvFromBackend({ base_url: "::::" })).length === 0);
ok("webEnv 空 backend → 空", Object.keys(webEnvFromBackend({})).length === 0);
// 默认端口（无显式端口）→ 不注入 AITR_WEB_PORT，后端用 config 默认
ok("webEnv 无端口不注入", webEnvFromBackend({ base_url: "https://example.com" }).AITR_WEB_PORT === undefined);

// 发布态但二进制缺失 → 回退（有 main.py 则 python）
const fallbackMain = path.join(REPO, "main.py");
const bundledMissing = resolveBackendSpawn({
  config: {}, isPackaged: true, resourcesPath: "/Resources",
  appDir: APP_DIR, platform: "linux", exists: (p) => p === fallbackMain,
});
ok("二进制缺失→回退 python", bundledMissing && bundledMissing.kind === "python");

// ── ④ 开发态：系统 Python 跑仓库根 main.py ───────────────────────────────────
const devWin = resolveBackendSpawn({
  config: {}, isPackaged: false, appDir: APP_DIR, platform: "win32",
  exists: (p) => p === fallbackMain,
});
ok("dev kind=python", devWin && devWin.kind === "python");
ok("dev win 默认 python", devWin.command === "python");
ok("dev args=main.py", devWin.args.join(",") === "main.py");
ok("dev cwd=仓库根", devWin.cwd === REPO);
ok("dev 不注入 env（零回归）", devWin.env && Object.keys(devWin.env).length === 0);

const devPosix = resolveBackendSpawn({
  config: {}, isPackaged: false, appDir: APP_DIR, platform: "darwin",
  exists: (p) => p === fallbackMain,
});
ok("dev posix 默认 python3", devPosix.command === "python3");

// 自定义 python 解释器
const devCustomPy = resolveBackendSpawn({
  config: { backend: { spawn: { python: "/usr/bin/python3.11" } } },
  isPackaged: false, appDir: APP_DIR, platform: "linux",
  exists: (p) => p === fallbackMain,
});
ok("dev 自定义 python", devCustomPy.command === "/usr/bin/python3.11");

// ── ⑤ 无任何产出 → null ───────────────────────────────────────────────────────
ok(
  "无 main.py 无二进制 → null",
  resolveBackendSpawn({
    config: {}, isPackaged: false, appDir: APP_DIR, platform: "win32", exists: () => false,
  }) === null
);

// ── ⑥ healthUrl 归一化 ───────────────────────────────────────────────────────
ok("healthUrl 默认", healthUrl({}) === "http://127.0.0.1:18799/login");
ok("healthUrl 去尾斜杠", healthUrl({ backend: { base_url: "http://x:9/" } }) === "http://x:9/login");

console.log(`backend-launcher.test.js: ${pass} passed`);

// ── ⑦ 生命周期：探活复用 + 幂等防重复 spawn（双实例竞态防线）─────────────────
//   双实例根治分两层：跨进程靠 main.js 的 Electron 单实例锁；同进程靠 start() 的
//   幂等卫（本节验证）——防 probe→spawn 的 TOCTOU 窗口被并发 start() 重复利用。
(async () => {
  let lpass = 0;
  function lok(name, cond) { assert.ok(cond, name); lpass++; }

  function fakeChild() {
    return { pid: 4242, stdout: { on() {} }, stderr: { on() {} }, on() {} };
  }
  const baseDeps = (over) => Object.assign({
    app: { isPackaged: false, getPath: () => "/tmp" },
    exec: () => {},
    fs: {
      existsSync: () => true,
      mkdirSync() {},
      createWriteStream: () => ({ write() {}, end() {} }),
    },
    log: () => {},
    readyTries: 2, readyIntervalMs: 1, // 单测加速：不等 90s
  }, over);
  const CFG = { backend: { base_url: "http://127.0.0.1:18799" } };

  // (a) 后端已可达 → running-external，绝不重复 spawn（零回归：复用既有/手动起的后端）
  {
    let spawned = 0;
    const mgr = createBackendManager(baseDeps({
      spawn: () => { spawned++; return fakeChild(); },
      fetch: async () => ({ status: 200 }), // 探活成功=已有后端在跑
    }));
    await mgr.start(CFG);
    lok("已有后端→不 spawn", spawned === 0);
    lok("已有后端→状态 running-external", mgr.getStatus().status === "running-external");
  }

  // (b) 无既有后端 + 并发二次进入 start() → 幂等卫保证只 spawn 一次（核心：防竞态僵尸）
  {
    let spawned = 0;
    const mgr = createBackendManager(baseDeps({
      spawn: () => { spawned++; return fakeChild(); },
      fetch: async () => { throw new Error("unreachable"); }, // 无既有后端→进入 spawn 路径
    }));
    await Promise.all([mgr.start(CFG), mgr.start(CFG)]);
    lok("并发 start → 只 spawn 一次", spawned === 1);
  }

  // (c) 已有存活子进程后再调 start() → 跳过（不二次 spawn）
  {
    let spawned = 0;
    const mgr = createBackendManager(baseDeps({
      spawn: () => { spawned++; return fakeChild(); },
      fetch: async () => { throw new Error("unreachable"); },
    }));
    await mgr.start(CFG);          // 第一次：spawn 一次（之后 child 存活）
    await mgr.start(CFG);          // 第二次：child 非空 → 跳过
    lok("child 存活→再次 start 不二次 spawn", spawned === 1);
  }

  console.log(`backend-launcher.test.js lifecycle: ${lpass} passed`);
})().catch((e) => { console.error(e); process.exit(1); });
