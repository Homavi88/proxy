/* oc-quota-widget.js — квотный pill в футере композера OpenCode Desktop (v12).
 * Поллит http://127.0.0.1:8081/quota, вставляет бейдж в строку контролов
 * футера композера (после "High", слева от кнопки отправки).
 * v12: точные якоря из бандла 1.18.26 (PromptInputV2):
 *   form[data-component=prompt-input-v2] > div.h-11.flex > div.flex-1 (левая группа)
 *   select-кнопки = button.max-w-[220px]; последняя = variant ("High").
 * pill инлайн после последней select; fallback — текстовый поиск в dock,
 * затем dock, затем fixed угол. Пока pill на fallback — перепыт каждый тик.
 * v12.2: удалена периодическая pill-debug телеметрия (спам в лог прокси).
 * v12.3: порт вынесен в константу PORT (смена порта — одна строка). */
(function () {
  "use strict";
  if (window.__ocQuotaWidget) return;
  window.__ocQuotaWidget = true;

  var WIDGET_VER = "12.3";
  var PORT = "8081"; // если прокси на другом порту — поменяй здесь
  var QUOTA_URL = "http://127.0.0.1:" + PORT + "/quota";
  var POLL_MS = 30000;
  var FETCH_TIMEOUT = 6000;
  var SHOW_STRAT_SUFFIX = false; // диагностический суффикс выключен (v12.1 финальная)
  var pill = null;
  var lastStrategy = "none";
  var lastFetch = "-";

  function fmtM(n) {
    if (n == null) return "?";
    if (n >= 1e8) return Math.round(n / 1e6) + "M";
    if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
    if (n >= 1e3) return Math.round(n / 1e3) + "K";
    return String(n);
  }

  function stratTag() {
    var dbg = window.__ocQuotaDebug || {};
    var t = "[" + lastStrategy;
    if (dbg.note) t += "," + dbg.note;
    t += "]";
    return SHOW_STRAT_SUFFIX ? " " + t : "";
  }

  function buildPill() {
    var d = document.createElement("div");
    d.id = "oc-quota-pill";
    d.style.cssText =
      "display:inline-flex;align-items:center;height:22px;padding:0 8px;" +
      "border-radius:999px;font-size:11px;line-height:1;white-space:nowrap;" +
      "background:rgba(128,128,128,0.14);color:var(--v2-text-text-muted,#666);" +
      "font-family:var(--v2-font-family-sans,system-ui,sans-serif);" +
      "user-select:none;flex:none;position:relative;z-index:10;";
    d.style.cursor = "pointer";
    d.textContent = "v12\u2026";
    d.addEventListener("click", function () {
      try { window.open("http://127.0.0.1:" + PORT + "/quota/dash", "_blank"); } catch (e) {}
    });
    return d;
  }

  function setPill(text, mode, title) {
    if (!pill) return;
    pill.textContent = text + stratTag();
    var bg = { ok: "rgba(128,128,128,0.14)", amber: "rgba(217,164,10,0.20)", red: "rgba(220,38,38,0.18)", gray: "rgba(128,128,128,0.10)", diag: "rgba(37,99,235,0.20)" }[mode] || "rgba(128,128,128,0.14)";
    var fg = { ok: "var(--v2-text-text-muted,#666)", amber: "#b45309", red: "#dc2626", gray: "var(--v2-text-text-faint,#999)", diag: "#3b82f6" }[mode] || "#666";
    pill.style.background = bg;
    pill.style.color = fg;
    if (title) pill.title = title + "\nКлик — открыть дашборд квоты";
  }

  function diagText() {
    var dbg = window.__ocQuotaDebug || {};
    var rc = dbg.row ? String(dbg.row.className || "").slice(0, 40) : "null";
    return "D " + lastStrategy + " | " + lastFetch + " | " + rc;
  }

  function render(data) {
    if (!pill || !data) return;
    var quota = data.quota;
    if (!quota) { setPill("quota off", "gray", "Квота в прокси не задана (HOURLY_TOKEN_QUOTA=0)"); return; }
    var used = data.window_total || 0;
    var pct = data.used_pct != null ? data.used_pct : (100 * used) / quota;
    var text = fmtM(used) + "/" + fmtM(quota) + " \u00b7 " + Math.round(pct) + "%";
    var title = "Квота токенов (" + (data.window_label || "") + "): " +
      used.toLocaleString() + " / " + quota.toLocaleString() + " (" + Math.round(pct) + "%).\n" +
      "Сессия: " + (data.session_total || 0).toLocaleString() + " токенов.\n" +
      "Осталось: " + (data.remaining || 0).toLocaleString();
    if (pct >= 95) setPill(text, "red", title);
    else if (pct >= 80) setPill(text, "amber", title);
    else setPill(text, "ok", title);
  }

  function isFlexRow(n) {
    var cs;
    try { cs = getComputedStyle(n); } catch (e) { return false; }
    return cs.display === "flex" &&
      (cs.flexDirection === "row" || cs.flexDirection === "row-reverse" || cs.flexDirection === "row-wrap");
  }

  function isGrid(n) {
    var cs;
    try { cs = getComputedStyle(n); } catch (e) { return false; }
    return cs.display === "grid";
  }

  function isShown(n) {
    var a = n;
    try {
      while (a && a !== document.documentElement) {
        var cs;
        try { cs = getComputedStyle(a); } catch (e) { return false; }
        if (cs.display === "none" || cs.visibility === "hidden" || cs.opacity === "0") return false;
        a = a.parentElement;
      }
    } catch (e) { return false; }
    try { return n.getClientRects ? n.getClientRects().length > 0 : false; } catch (e) { return false; }
  }

  function pickDock() {
    var docks;
    try { docks = document.querySelectorAll('[data-component="session-prompt-dock"]'); } catch (e) { return null; }
    for (var i = 0; i < docks.length; i++) {
      if (isShown(docks[i])) return docks[i];
    }
    return null;
  }

  // Дамп цепочки предков: tag:классы(14):display — для диагностики в тексте pill.
  function chainInfo(startEl, levels) {
    var parts = [];
    var a = startEl ? startEl.parentElement : null;
    for (var k = 0; k < levels && a && a !== document.body && a !== document.documentElement; k++) {
      var tag = "?", c = "", d = "?";
      try { tag = a.tagName ? a.tagName.toLowerCase() : "?"; } catch (e) {}
      try { c = String(a.className == null ? "" : a.className).replace(/\s+/g, ".").slice(0, 14); } catch (e) { c = "?"; }
      try {
        var cs = getComputedStyle(a);
        var dd = cs.display;
        if (dd === "flex") d = "f";
        else if (dd === "inline-flex") d = "F";
        else if (dd === "grid") d = "g";
        else if (dd === "block") d = "b";
        else if (dd === "inline") d = "i";
        else if (dd === "none") d = "n";
        else d = String(dd).slice(0, 1) || "?";
      } catch (e) { d = "?"; }
      parts.push(tag + ":" + c + ":" + d);
      a = a.parentElement;
    }
    return parts.join(">");
  }

  // Найти видимый элемент-контрол по тексту и flex-строку, которая его содержит.
  // "High" имеет приоритет. Результат: { el: <контрол>, row: <строка|null>, note: "..." }
  function findControl(scope, stopAt, up) {
    var WANT = ["High", "Medium", "Low", "Build", "qa-api-model"];
    var out = { el: null, row: null, note: "" };
    var els = null;
    try { els = scope.querySelectorAll("button, [role='button'], div, span, a"); } catch (e) { return out; }
    var highEl = null, best = null, bestText = "";
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      var t = "";
      try { t = (el.textContent || "").trim(); } catch (e) { continue; }
      if (!t || t.length > 40) continue;
      var want = false;
      for (var w = 0; w < WANT.length; w++) {
        if (t === WANT[w] || (t.indexOf(WANT[w]) === 0 && t.length < WANT[w].length + 3)) { want = true; break; }
      }
      if (!want) continue;
      if (!isShown(el)) continue;
      if (t === "High" && !highEl) highEl = el;
      best = el; bestText = WANT[w];
    }
    if (highEl) { best = highEl; bestText = "High"; }
    if (!best) return out;
    out.el = best;
    out.note = "u" + up + ",t:" + bestText;
    var r = best.parentElement;
    for (var k = 0; k < 8 && r && r !== stopAt && r !== document.body; k++, r = r.parentElement) {
      if (r.children.length >= 2 && isShown(r) && (isFlexRow(r) || isGrid(r))) {
        out.row = r;
        return out;
      }
    }
    out.note += ",noflex,C:" + chainInfo(best, 6);
    return out;
  }

  // v12: точные якоря из бандла 1.18.26 (PromptInputV2):
  //   form[data-component=prompt-input-v2]
  //     > div.flex.h-11.items-center.px-2              (строка футера)
  //       > div.flex.min-w-0.flex-1.items-center.gap-1 (левая группа контролов)
  //   select = button.max-w-[220px] (MenuV2.Trigger); порядок: +, agent, model, variant.
  //   Последняя select = variant ("High") — pill вешаем сразу после неё.
  function findRow() {
    var dbg = window.__ocQuotaDebug || (window.__ocQuotaDebug = {});
    dbg.note = "";
    dbg.afterEl = null;
    dbg.anchorEl = null;

    // 1) точный v2-якорь
    var forms = document.querySelectorAll('[data-component="prompt-input-v2"]');
    for (var f = 0; f < forms.length; f++) {
      var form = forms[f];
      if (!isShown(form)) continue;
      var divs;
      try { divs = form.querySelectorAll("div"); } catch (e) { continue; }
      var row = null;
      for (var i = 0; i < divs.length; i++) {
        var c = divs[i].classList;
        if (c && c.contains("h-11") && c.contains("flex") && c.contains("items-center") && isShown(divs[i])) { row = divs[i]; break; }
      }
      if (!row) continue;
      var left = null;
      for (var j = 0; j < row.children.length; j++) {
        var ch = row.children[j];
        if (ch.classList && ch.classList.contains("flex-1") && ch.classList.contains("flex")) { left = ch; break; }
      }
      var host = left || row;
      var btns;
      try { btns = row.querySelectorAll("button"); } catch (e) { btns = []; }
      var selects = [];
      for (var b = 0; b < btns.length; b++) {
        if (btns[b].classList && btns[b].classList.contains("max-w-[220px]")) selects.push(btns[b]);
      }
      var after = selects.length ? selects[selects.length - 1] : null;
      var aLabel = "";
      try { aLabel = String((after && after.textContent) || "").trim().slice(0, 12).replace(/\s+/g, "."); } catch (e) {}
      if (dbg.note) dbg.note += ";";
      dbg.note += "v2,s" + selects.length + (after ? ",a:" + aLabel : "");
      var anchor = after ? climbToRowChild(after, host) : null;
      dbg.afterEl = anchor || (after && host.contains(after) ? after : null);
      lastStrategy = "v2";
      return host;
    }

    // 2) fallback: видимый текст контрола — строго внутри dock (не выше!)
    var dock = pickDock();
    if (dock) {
      var fc = findControl(dock, dock.parentElement || null, 0);
      if (fc.el && fc.row) {
        if (dbg.note) dbg.note += ";";
        dbg.note += fc.note;
        dbg.anchorEl = fc.el;
        var x = climbToRowChild(fc.el, fc.row);
        if (x) dbg.afterEl = x;
        lastStrategy = "text";
        return fc.row;
      }
      // строка контролов h-7 gap-1.5 (старые сборки)
      var rows = dock.querySelectorAll('div[class*="h-7"][class*="gap-1.5"]');
      for (var r = rows.length - 1; r >= 0; r--) {
        var cls = String(rows[r].className || "");
        if (cls.indexOf("absolute") !== -1) continue;
        if (isShown(rows[r])) {
          if (dbg.note) dbg.note += ";";
          dbg.note += "ctl";
          lastStrategy = "controls";
          return rows[r];
        }
      }
      if (dbg.note) dbg.note += ";dock"; else dbg.note = "dock";
      lastStrategy = "dock";
      return dock;
    }
    return null; // фиксированный угол — в attach()
  }

  // --- Скрытие, пока открыто меню/поповер ---
  var MENU_OPEN_SELECTOR =
    '[data-slot="select-v2-content"],[data-component="menu-v2-content"],' +
    '[data-component="select-content"],[data-component="popover-content"],' +
    '[data-component="dropdown-menu-content"],[data-component="context-menu-content"]';

  function isVisibleEl(el) {
    if (!el) return false;
    var cs;
    try { cs = getComputedStyle(el); } catch (e) { return true; }
    if (cs.display === "none" || cs.visibility === "hidden" || cs.opacity === "0") return false;
    try { return el.getClientRects ? el.getClientRects().length > 0 : true; } catch (e) { return true; }
  }

  function intersects(a, b, m) {
    return a.right >= b.left - m && a.left <= b.right + m &&
           a.bottom >= b.top - m && a.top <= b.bottom + m;
  }

  function menuCoversPill() {
    var els;
    try { els = document.querySelectorAll(MENU_OPEN_SELECTOR); } catch (e) { return false; }
    if (!els.length) return false;
    var pr;
    try { pr = pill.getBoundingClientRect(); } catch (e) { return false; }
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (!isVisibleEl(el)) continue;
      var r;
      try { r = el.getBoundingClientRect(); } catch (e) { continue; }
      if (r.width === 0 && r.height === 0) continue;
      if (intersects(r, pr, 8)) return true;
    }
    return false;
  }

  function updateVisibility() {
    if (!pill || !pill.isConnected) return;
    pill.style.visibility = menuCoversPill() ? "hidden" : "visible";
  }

  var visScheduled = false;
  function scheduleVisibility() {
    if (visScheduled) return;
    visScheduled = true;
    var run = function () { visScheduled = false; try { updateVisibility(); } catch (e) {} };
    try { (typeof requestAnimationFrame === "function") ? requestAnimationFrame(run) : run(); } catch (e) { run(); }
  }

  // Поднять элемент до прямого child строки (обёртка контрола).
  function climbToRowChild(el, row) {
    var x = el;
    try {
      while (x && x.parentElement && x.parentElement !== row) x = x.parentElement;
      if (x && x !== row && row.contains(x)) return x;
    } catch (e) {}
    return null;
  }

  function attach() {
    var dbg = window.__ocQuotaDebug || (window.__ocQuotaDebug = {});
    // на fallback-позиции (dock/fixed) — перепытывать строку каждый тик
    var inferior = (lastStrategy === "dock" || lastStrategy === "fixed");
    if (pill && pill.isConnected) {
      var ok = false;
      try {
        if (dbg.row && isShown(dbg.row) && dbg.row.contains(pill)) {
          if (dbg.afterEl) {
            ok = (pill.previousElementSibling === dbg.afterEl);
          } else {
            var b = pill.getBoundingClientRect();
            ok = b.width > 0 && b.height > 0;
          }
        }
      } catch (e) {}
      if (ok && !inferior) { updateVisibility(); return true; }
      try { if (pill.parentNode) pill.parentNode.removeChild(pill); } catch (e) {}
    }
    var row = findRow();
    if (row) {
      if (!pill) pill = buildPill();
      // сбрасываем позиционирование от старых версий
      try {
        pill.style.position = "relative";
        pill.style.left = "";
        pill.style.top = "";
        pill.style.transform = "";
        pill.style.marginLeft = "";
        pill.style.right = "";
        pill.style.bottom = "";
        pill.style.zIndex = "";
      } catch (e) {}
      try {
        if (dbg.afterEl && row.contains(dbg.afterEl)) {
          row.insertBefore(pill, dbg.afterEl.nextSibling);
        } else {
          row.appendChild(pill);
        }
      } catch (e) {
        try { row.appendChild(pill); } catch (e2) {}
      }
      dbg.row = row;
    } else if (pill) {
      try { if (pill.parentNode) pill.parentNode.removeChild(pill); } catch (e) {}
      document.body.appendChild(pill);
      pill.style.position = "fixed";
      pill.style.right = "16px";
      pill.style.bottom = "14px";
      pill.style.zIndex = "2147483000";
      lastStrategy = "fixed";
      dbg.row = null;
      dbg.afterEl = null;
    }
    if (pill) updateVisibility();
    return !!(pill && pill.isConnected);
  }

  function maintenance() {
    try { attach(); } catch (e) { /* никогда не роняем приложение */ }
  }

  function tick() {
    try {
      if (!attach()) return;
      setPill("D " + lastStrategy + " | load", "diag", diagText());
      var ctrl = null;
      try { ctrl = (typeof AbortController === "function") ? new AbortController() : null; } catch (e) { ctrl = null; }
      var to = null;
      if (ctrl) { try { to = setTimeout(function () { try { ctrl.abort(); } catch (e) {} }, FETCH_TIMEOUT); } catch (e) {} }
      var opts = { cache: "no-store" };
      if (ctrl && ctrl.signal) opts.signal = ctrl.signal;
      fetch(QUOTA_URL, opts)
        .then(function (r) {
          lastFetch = "http" + r.status;
          if (to) { try { clearTimeout(to); } catch (e) {} }
          if (!r.ok) throw new Error("http" + r.status);
          return r.json();
        })
        .then(function (d) {
          if (d && d.ok) { lastFetch = "ok"; render(d); }
          else { lastFetch = "badjson"; setPill(diagText(), "diag", diagText()); }
        })
        .catch(function (err) {
          lastFetch = (err && err.name === "AbortError") ? "timeout" : "err";
          if (to) { try { clearTimeout(to); } catch (e) {} }
          setPill(diagText(), "diag", "Ошибка: " + (err && err.message ? err.message : "fetch") + "\n" + diagText());
        });
    } catch (e) {
      lastFetch = "throw";
      if (pill) setPill(diagText(), "diag", "throw: " + (e && e.message ? e.message : "?"));
    }
  }

  try {
    if (typeof MutationObserver === "function") {
      new MutationObserver(scheduleVisibility).observe(document.documentElement, { childList: true, subtree: true });
    }
  } catch (e) { /* нет observer — ловим через интервал ниже */ }

  setInterval(tick, POLL_MS);
  setInterval(maintenance, 500); // переподключить (Solid перерисовал футер) + sync видимости
  setTimeout(tick, 1500);
  setTimeout(tick, 8000);
})();
