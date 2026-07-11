"use strict";

// 首启向导（P0-1）：仅在首次运行（无 localStorage 标记）时弹一次。
// 步骤：① 界面语言 + 后台令牌 → ② 填 AI Key（测试 / 保存到后端 overlay）→ ③ 翻译就绪绿灯。
// AI 已配置（升级/重装保留数据目录）时只走 ①（与旧行为一致）。
// 流程编排/结果映射为纯函数（first-run-model.js，可 Node 单测）；本文件只做 DOM + IPC。
// 自包含、不依赖 renderer 内部；CSP: script-src 'self' 故为独立文件，样式走 inline（'unsafe-inline' 已许可）。
(function () {
  var FLAG = "aitr_firstrun_v1";
  try {
    if (localStorage.getItem(FLAG)) return;
  } catch (e) { /* localStorage 不可用：仍展示一次（不持久化） */ }

  var INPUT_STYLE = "width:100%;box-sizing:border-box;padding:8px 10px;background:#0f1420;color:#e6e9ef;border:1px solid #2a3344;border-radius:8px";
  var LABEL_STYLE = "display:block;font-size:13px;margin-bottom:6px;color:#c4ccd9";
  var BTN_PRIMARY = "padding:8px 18px;background:#2563eb;color:#fff;border:0;border-radius:8px;cursor:pointer;font-weight:600";
  var BTN_GHOST = "padding:8px 16px;background:transparent;color:#97a3b6;border:1px solid #2a3344;border-radius:8px;cursor:pointer";

  function run() {
    var shell = window.shell || {};
    var cfgPromise = shell.getConfig ? shell.getConfig() : Promise.resolve({});
    var aiPromise = shell.setupAiStatus ? shell.setupAiStatus().catch(function () { return null; }) : Promise.resolve(null);
    Promise.all([Promise.resolve(cfgPromise), Promise.resolve(aiPromise)]).then(function (rs) {
      var cfg = rs[0] || {};
      var aiStatus = rs[1];
      var curToken = (cfg.backend && cfg.backend.token) || "admin";
      var curLang = (cfg.unified_inbox && cfg.unified_inbox.lang) || "";

      var steps = window.frBuildSteps ? window.frBuildSteps(aiStatus) : ["basic"];
      var prefill = window.frAiPrefill ? window.frAiPrefill(aiStatus) : { base_url: "", model: "" };
      var state = { lang: curLang, token: curToken, saveView: null };

      var mask = document.createElement("div");
      mask.setAttribute("style", [
        "position:fixed;inset:0;z-index:99999",
        "background:rgba(8,12,20,.78);backdrop-filter:blur(2px)",
        "display:flex;align-items:center;justify-content:center",
        "font-family:system-ui,'Microsoft YaHei',sans-serif"
      ].join(";"));

      var card = document.createElement("div");
      card.setAttribute("style", [
        "width:440px;max-width:90vw;background:#161b26;color:#e6e9ef",
        "border:1px solid #2a3344;border-radius:12px;padding:24px 26px",
        "box-shadow:0 18px 60px rgba(0,0,0,.5)"
      ].join(";"));
      mask.appendChild(card);
      document.body.appendChild(mask);

      function t(key) { return window.frT ? window.frT(state.lang, key) : key; }

      function finish(save) {
        try { localStorage.setItem(FLAG, "1"); } catch (e) {}
        var fin = Promise.resolve();
        if (save && shell.saveConfig) {
          fin = Promise.resolve(shell.saveConfig({
            unified_inbox: { lang: state.lang },
            backend: { token: (state.token || "admin").trim() }
          })).catch(function () {});
        }
        fin.then(function () {
          if (save) { try { location.reload(); return; } catch (e) {} }
          if (mask.parentNode) mask.parentNode.removeChild(mask);
        });
      }

      // ── 步骤 ①：语言 + 令牌（与旧版一致；有 AI 步时按钮变「下一步」） ──
      function renderBasic() {
        var hasAiStep = steps.indexOf("ai") >= 0;
        card.innerHTML =
          '<div style="font-size:18px;font-weight:600;margin-bottom:6px">欢迎使用 AI 客服桌面端</div>' +
          '<div style="font-size:13px;color:#97a3b6;line-height:1.6;margin-bottom:18px">' +
          '首次启动会自动拉起本地后台服务（无需手动运行 Python），稍等片刻即可使用。<br>下面选项可先确认，之后也能在设置里改。</div>' +
          '<label style="' + LABEL_STYLE + '">界面语言</label>' +
          '<select id="fr-lang" style="' + INPUT_STYLE + ';margin-bottom:16px">' +
          '<option value="">跟随后台</option><option value="zh">中文</option><option value="en">English</option></select>' +
          '<label style="' + LABEL_STYLE + '">后台访问令牌（默认 admin）</label>' +
          '<input id="fr-token" type="text" style="' + INPUT_STYLE + ';margin-bottom:22px" />' +
          '<div style="display:flex;gap:10px;justify-content:flex-end">' +
          '<button id="fr-skip" style="' + BTN_GHOST + '">跳过</button>' +
          '<button id="fr-ok" style="' + BTN_PRIMARY + '">' + (hasAiStep ? "下一步" : "完成并进入") + '</button>' +
          '</div>';
        var langSel = card.querySelector("#fr-lang");
        var tokenInp = card.querySelector("#fr-token");
        langSel.value = state.lang;
        tokenInp.value = state.token;
        card.querySelector("#fr-ok").addEventListener("click", function () {
          state.lang = langSel.value;
          state.token = tokenInp.value;
          if (hasAiStep) renderAi();
          else finish(true);
        });
        card.querySelector("#fr-skip").addEventListener("click", function () { finish(false); });
      }

      // ── 步骤 ②（A2）：AI Key 填写 → 测试（POST /api/setup/test-ai）→ 保存（POST /api/setup/ai-key，写 overlay） ──
      function renderAi() {
        card.innerHTML =
          '<div style="font-size:17px;font-weight:600;margin-bottom:6px">' + t("ai_title") + '</div>' +
          '<div style="font-size:12px;color:#97a3b6;line-height:1.6;margin-bottom:16px">' + t("ai_sub") + '</div>' +
          '<label style="' + LABEL_STYLE + '">' + t("ai_key_label") + '</label>' +
          '<input id="fr-ai-key" type="password" autocomplete="off" style="' + INPUT_STYLE + ';margin-bottom:12px" />' +
          '<label style="' + LABEL_STYLE + '">' + t("ai_base_label") + '</label>' +
          '<input id="fr-ai-base" type="text" style="' + INPUT_STYLE + ';margin-bottom:12px" />' +
          '<label style="' + LABEL_STYLE + '">' + t("ai_model_label") + '</label>' +
          '<input id="fr-ai-model" type="text" style="' + INPUT_STYLE + ';margin-bottom:10px" />' +
          '<div id="fr-ai-msg" style="min-height:18px;font-size:12px;margin-bottom:12px"></div>' +
          '<div style="display:flex;gap:10px;justify-content:flex-end;align-items:center">' +
          '<button id="fr-ai-back" style="' + BTN_GHOST + '">' + t("btn_back") + '</button>' +
          '<button id="fr-ai-skip" style="' + BTN_GHOST + '">' + t("btn_skip") + '</button>' +
          '<button id="fr-ai-test" style="' + BTN_GHOST + ';color:#e6e9ef">' + t("btn_test") + '</button>' +
          '<button id="fr-ai-save" style="' + BTN_PRIMARY + '">' + t("btn_save") + '</button>' +
          '</div>';
        var keyInp = card.querySelector("#fr-ai-key");
        var baseInp = card.querySelector("#fr-ai-base");
        var modelInp = card.querySelector("#fr-ai-model");
        var msg = card.querySelector("#fr-ai-msg");
        baseInp.value = prefill.base_url || "";
        modelInp.value = prefill.model || "";

        function setMsg(cls, text) {
          msg.textContent = text || "";
          msg.style.color = cls === "ok" ? "#34d399" : (cls === "warn" ? "#fbbf24" : (cls === "err" ? "#f87171" : "#97a3b6"));
        }
        function vals() {
          return {
            api_key: (keyInp.value || "").trim(),
            base_url: (baseInp.value || "").trim(),
            model: (modelInp.value || "").trim()
          };
        }
        card.querySelector("#fr-ai-back").addEventListener("click", renderBasic);
        card.querySelector("#fr-ai-skip").addEventListener("click", function () { finish(true); });
        card.querySelector("#fr-ai-test").addEventListener("click", function () {
          var v = vals();
          var chk = window.frValidateAiInput ? window.frValidateAiInput(v) : { ok: true };
          if (!chk.ok) { setMsg("err", t(chk.err)); return; }
          setMsg("", t("testing"));
          var p = shell.setupTestAi ? shell.setupTestAi(v) : Promise.resolve(null);
          Promise.resolve(p).then(function (resp) {
            var view = window.frAiTestView ? window.frAiTestView(resp, state.lang) : { cls: "err", text: t("test_fail") };
            setMsg(view.cls, view.text);
          }).catch(function () { setMsg("err", t("backend_wait")); });
        });
        card.querySelector("#fr-ai-save").addEventListener("click", function () {
          var v = vals();
          var chk = window.frValidateAiInput ? window.frValidateAiInput(v) : { ok: true };
          if (!chk.ok) { setMsg("err", t(chk.err)); return; }
          setMsg("", t("saving"));
          var p = shell.setupSaveAiKey ? shell.setupSaveAiKey(v) : Promise.resolve(null);
          Promise.resolve(p).then(function (resp) {
            var view = window.frAiSaveView ? window.frAiSaveView(resp, state.lang) : { cls: "err", ready: false, text: t("save_fail") };
            if (view.cls === "err") { setMsg("err", view.text); return; }
            state.saveView = view;
            renderResult();
          }).catch(function () { setMsg("err", t("backend_wait")); });
        });
      }

      // ── 步骤 ③（A5）：翻译就绪绿灯 / 失败下一步指引 ──
      function renderResult() {
        var view = window.frResultView ? window.frResultView(state.saveView, state.lang)
          : { cls: "warn", title: "", sub: "" };
        var okGreen = view.cls === "ok";
        card.innerHTML =
          '<div style="text-align:center;padding:8px 0 4px">' +
          '<div style="font-size:44px;line-height:1;margin-bottom:12px">' + (okGreen ? "🟢" : "🟡") + '</div>' +
          '<div style="font-size:18px;font-weight:600;margin-bottom:8px;color:' + (okGreen ? "#34d399" : "#fbbf24") + '">' + view.title + '</div>' +
          '<div style="font-size:13px;color:#97a3b6;line-height:1.7;margin-bottom:20px">' + view.sub + '</div>' +
          '<button id="fr-done" style="' + BTN_PRIMARY + ';width:100%">' + t("btn_finish") + '</button>' +
          '</div>';
        card.querySelector("#fr-done").addEventListener("click", function () { finish(true); });
      }

      renderBasic();
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", run);
  else run();
})();
