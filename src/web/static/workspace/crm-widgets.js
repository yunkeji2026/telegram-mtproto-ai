/* 工作台共享前端组件（Phase 6-13 抽取）
 * 把分散在 workspace_base / workspace_dashboard / unified_inbox 等模板里重复的
 * 纯工具函数（格式化 / 迷你折线 / toast）收敛到一处，挂在 window.CRMW。
 * 设计：零依赖、纯函数、幂等；模板里的同名函数改为委托到这里，调用点零改动。
 */
(function () {
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function _unit(key, n) {
    n = n == null ? '' : String(n);
    if (typeof window.Tf === 'function') return window.Tf(key, {n: n});
    return n;
  }

  // 带一位小数的时长（秒/分/时）——用于"平均首响"等聚合展示
  function fmtDur(sec) {
    sec = sec | 0;
    if (sec < 60) return _unit('crmw.unit.sec', sec);
    if (sec < 3600) return _unit('crmw.unit.min_dec', (sec / 60).toFixed(1));
    return _unit('crmw.unit.hour_dec', (sec / 3600).toFixed(1));
  }

  // 整数粒度等待时长（秒/分/时/天）——会话列表 ⏱
  function fmtWait(sec) {
    sec = sec | 0;
    if (sec < 60) return _unit('crmw.unit.sec', sec);
    if (sec < 3600) return _unit('crmw.unit.min', Math.floor(sec / 60));
    if (sec < 86400) return _unit('crmw.unit.hour', Math.floor(sec / 3600));
    return _unit('crmw.unit.day', Math.floor(sec / 86400));
  }

  // 分钟起步粒度（分/时/天）——顶栏 SLA 徽标/告警
  function fmtWaitMin(sec) {
    sec = sec | 0;
    if (sec < 3600) return _unit('crmw.unit.min', Math.floor(sec / 60));
    if (sec < 86400) return _unit('crmw.unit.hour', Math.floor(sec / 3600));
    return _unit('crmw.unit.day', Math.floor(sec / 86400));
  }

  // 自适应纵轴折线（rows:[{day,...}]，key 取值字段）
  function spark(rows, key, color) {
    var W = 560, H = 90, pad = 18, n = rows.length;
    var vals = rows.map(function (r) { return r[key] || 0; });
    var mx = Math.max(1, Math.max.apply(null, vals));
    var dx = n > 1 ? (W - 2 * pad) / (n - 1) : 0;
    var pts = vals.map(function (v, i) {
      var x = pad + dx * i, y = H - pad - (v / mx) * (H - 2 * pad);
      return [x, y, v, rows[i].day];
    });
    var poly = pts.map(function (p) { return p[0].toFixed(1) + ',' + p[1].toFixed(1); }).join(' ');
    var dots = pts.map(function (p) {
      return '<circle cx="' + p[0].toFixed(1) + '" cy="' + p[1].toFixed(1) +
        '" r="3" style="fill:' + color + ';"><title>' + esc(p[3]) + ': ' + p[2] + '</title></circle>';
    }).join('');
    var lbls = pts.map(function (p) {
      return '<text x="' + p[0].toFixed(1) + '" y="' + (H - 4) +
        '" font-size="9" text-anchor="middle" style="fill:var(--tk-text-muted);">' + esc(p[3]) + '</text>';
    }).join('');
    return '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width:100%;height:90px;">' +
      '<polyline points="' + poly + '" style="fill:none;stroke:' + color + ';stroke-width:2;"/>' +
      dots + lbls + '</svg>';
  }

  // 固定 0–100% 纵轴折线（达标率等百分比）
  function sparkPct(rows, key, color) {
    var W = 560, H = 90, pad = 18, n = rows.length, mx = 100;
    var dx = n > 1 ? (W - 2 * pad) / (n - 1) : 0;
    var pts = rows.map(function (r, i) {
      var v = r[key] || 0; var x = pad + dx * i, y = H - pad - (v / mx) * (H - 2 * pad);
      return [x, y, v, r.day];
    });
    var poly = pts.map(function (p) { return p[0].toFixed(1) + ',' + p[1].toFixed(1); }).join(' ');
    var dots = pts.map(function (p) {
      return '<circle cx="' + p[0].toFixed(1) + '" cy="' + p[1].toFixed(1) +
        '" r="3" style="fill:' + color + ';"><title>' + esc(p[3]) + ': ' + p[2] + '%</title></circle>';
    }).join('');
    var lbls = pts.map(function (p) {
      return '<text x="' + p[0].toFixed(1) + '" y="' + (H - 4) +
        '" font-size="9" text-anchor="middle" style="fill:var(--tk-text-muted);">' + esc(p[3]) + '</text>';
    }).join('');
    return '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width:100%;height:90px;">' +
      '<line x1="' + pad + '" y1="' + pad + '" x2="' + (W - pad) + '" y2="' + pad + '" style="stroke:var(--tk-border);"/>' +
      '<polyline points="' + poly + '" style="fill:none;stroke:' + color + ';stroke-width:2;"/>' +
      dots + lbls + '</svg>';
  }

  // 右下角轻量 toast（自动消失，可点关闭）
  // 第二参数：历史 hex 色值 → 语义类；省略时默认 info（蓝），不再误用红底
  var _TOAST_KINDS = {
    err: ['#dc2626', '#ef4444', '#b91c1c', '#991b1b', '#9d174d', '#be185d', '#f87171'],
    ok: ['#16a34a', '#15803d', '#0f766e', '#0d9488', '#22c55e', '#10b981'],
    warn: ['#d97706', '#b45309', '#f59e0b', '#92400e'],
    info: ['#2563eb', '#3b82f6', '#1d4ed8'],
    muted: ['#64748b', '#94a3b8', '#6b7280'],
    vio: ['#7c3aed', '#8b5cf6', '#6366f1']
  };
  function _toastKind(color) {
    if (!color) return 'info';
    var c = String(color).toLowerCase();
    for (var k in _TOAST_KINDS) {
      if (_TOAST_KINDS[k].indexOf(c) >= 0) return k;
    }
    return null;
  }

  function toast(text, color, icon) {
    var box = document.getElementById('ws-toast-box');
    if (!box) {
      box = document.createElement('div');
      box.id = 'ws-toast-box';
      box.className = 'tk-toast-wrap';
      document.body.appendChild(box);
    }
    var t = document.createElement('div');
    var kind = _toastKind(color);
    if (kind) {
      t.className = 'tk-toast ' + kind;
    } else if (color) {
      t.className = 'tk-toast';
      t.style.background = color;
    } else {
      t.className = 'tk-toast info';
    }
    if (icon && typeof window.uiIcon === 'function') {
      t.style.display = 'inline-flex';
      t.style.alignItems = 'flex-start';
      t.style.gap = '7px';
      t.innerHTML = window.uiIcon(icon, 14) +
        '<span style="flex:1;min-width:0;">' + esc(text) + '</span>';
    } else {
      t.textContent = text;
    }
    t.onclick = function () { try { box.removeChild(t); } catch (_) { } };
    box.appendChild(t);
    setTimeout(function () { try { box.removeChild(t); } catch (_) { } }, 8000);
    return t;  // 返回元素，便于调用方覆写 onclick（如点击通知跳转会话）
  }

  window.CRMW = {
    esc: esc, fmtDur: fmtDur, fmtWait: fmtWait, fmtWaitMin: fmtWaitMin,
    spark: spark, sparkPct: sparkPct, toast: toast,
  };
})();
