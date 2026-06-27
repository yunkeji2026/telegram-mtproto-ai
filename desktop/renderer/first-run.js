"use strict";

// 首启向导（P0 双击即用收尾）：仅在首次运行（无 localStorage 标记）时弹一次，
// 收集「界面语言 + 后台访问令牌」，写回 config.json 后重载使 renderer 以新配置初始化。
// 自包含、不依赖 renderer 内部；CSP: script-src 'self' 故为独立文件，样式走 inline（'unsafe-inline' 已许可）。
(function () {
  var FLAG = "aitr_firstrun_v1";
  try {
    if (localStorage.getItem(FLAG)) return;
  } catch (e) { /* localStorage 不可用：仍展示一次（不持久化） */ }

  function run() {
    var shell = window.shell || {};
    var cfgPromise = shell.getConfig ? shell.getConfig() : Promise.resolve({});
    Promise.resolve(cfgPromise).then(function (cfg) {
      cfg = cfg || {};
      var curToken = (cfg.backend && cfg.backend.token) || "admin";
      var curLang = (cfg.unified_inbox && cfg.unified_inbox.lang) || "";

      var mask = document.createElement("div");
      mask.setAttribute("style", [
        "position:fixed;inset:0;z-index:99999",
        "background:rgba(8,12,20,.78);backdrop-filter:blur(2px)",
        "display:flex;align-items:center;justify-content:center",
        "font-family:system-ui,'Microsoft YaHei',sans-serif"
      ].join(";"));

      var card = document.createElement("div");
      card.setAttribute("style", [
        "width:420px;max-width:90vw;background:#161b26;color:#e6e9ef",
        "border:1px solid #2a3344;border-radius:12px;padding:24px 26px",
        "box-shadow:0 18px 60px rgba(0,0,0,.5)"
      ].join(";"));
      card.innerHTML =
        '<div style="font-size:18px;font-weight:600;margin-bottom:6px">欢迎使用 AI 客服桌面端</div>' +
        '<div style="font-size:13px;color:#97a3b6;line-height:1.6;margin-bottom:18px">' +
        '首次启动会自动拉起本地后台服务（无需手动运行 Python），稍等片刻即可使用。<br>下面两项可先确认，之后也能在设置里改。</div>' +
        '<label style="display:block;font-size:13px;margin-bottom:6px;color:#c4ccd9">界面语言</label>' +
        '<select id="fr-lang" style="width:100%;padding:8px 10px;margin-bottom:16px;background:#0f1420;color:#e6e9ef;border:1px solid #2a3344;border-radius:8px">' +
        '<option value="">跟随后台</option><option value="zh">中文</option><option value="en">English</option></select>' +
        '<label style="display:block;font-size:13px;margin-bottom:6px;color:#c4ccd9">后台访问令牌（默认 admin）</label>' +
        '<input id="fr-token" type="text" style="width:100%;box-sizing:border-box;padding:8px 10px;margin-bottom:22px;background:#0f1420;color:#e6e9ef;border:1px solid #2a3344;border-radius:8px" />' +
        '<div style="display:flex;gap:10px;justify-content:flex-end">' +
        '<button id="fr-skip" style="padding:8px 16px;background:transparent;color:#97a3b6;border:1px solid #2a3344;border-radius:8px;cursor:pointer">跳过</button>' +
        '<button id="fr-ok" style="padding:8px 18px;background:#2563eb;color:#fff;border:0;border-radius:8px;cursor:pointer;font-weight:600">完成并进入</button>' +
        '</div>';

      mask.appendChild(card);
      document.body.appendChild(mask);

      var langSel = card.querySelector("#fr-lang");
      var tokenInp = card.querySelector("#fr-token");
      langSel.value = curLang;
      tokenInp.value = curToken;

      function done(save) {
        try { localStorage.setItem(FLAG, "1"); } catch (e) {}
        var fin = Promise.resolve();
        if (save && shell.saveConfig) {
          fin = Promise.resolve(shell.saveConfig({
            unified_inbox: { lang: langSel.value },
            backend: { token: (tokenInp.value || "admin").trim() }
          })).catch(function () {});
        }
        fin.then(function () {
          if (save) { try { location.reload(); return; } catch (e) {} }
          if (mask.parentNode) mask.parentNode.removeChild(mask);
        });
      }

      card.querySelector("#fr-ok").addEventListener("click", function () { done(true); });
      card.querySelector("#fr-skip").addEventListener("click", function () { done(false); });
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", run);
  else run();
})();
