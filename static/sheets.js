/* Argos Fogli collaborativi — editor griglia (vanilla JS, zero dipendenze).
 *
 * Architettura a 3 livelli, pensata per il realtime:
 *   - SheetModel : stato celle + revisione.
 *   - SheetGrid  : rendering DOM + interazione (selezione, edit, tastiera, copia/incolla).
 *   - Transport  : I/O col server. HttpTransport (Fase 2) e WsTransport (Fase 3+)
 *                  implementano la stessa interfaccia; la griglia non sa quale usa.
 *
 * Il server e' la fonte di verita': ogni patch torna con una `revision` monotona.
 * La griglia applica le patch REMOTE (di altri utenti) e tiene `lastRevision`
 * per il recupero al reconnect.
 */
(function () {
  "use strict";

  var cfg = JSON.parse(document.getElementById("sheet-config").textContent);

  // ---- helpers -----------------------------------------------------------
  function colLabel(n) {
    var s = "";
    n += 1;
    while (n > 0) {
      var m = (n - 1) % 26;
      s = String.fromCharCode(65 + m) + s;
      n = Math.floor((n - 1) / 26);
    }
    return s;
  }
  function key(r, c) { return r + "," + c; }
  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
  var COLORS = ["#38bdf8", "#f59e0b", "#22c55e", "#ef4444", "#a855f7",
                "#ec4899", "#14b8a6", "#f97316", "#6366f1", "#84cc16"];
  function colorFor(uid) { return COLORS[((uid || 0) % COLORS.length + COLORS.length) % COLORS.length]; }

  // ---- model -------------------------------------------------------------
  function SheetModel(nRows, nCols) {
    this.nRows = nRows;
    this.nCols = nCols;
    this.cells = {};      // "r,c" -> {value, formula, style}
    this.revision = 0;
  }
  SheetModel.prototype.get = function (r, c) { return this.cells[key(r, c)] || null; };
  SheetModel.prototype.setCell = function (r, c, value, formula, style) {
    var k = key(r, c);
    if ((value === null || value === "" || value === undefined) && !formula) {
      delete this.cells[k];
    } else {
      this.cells[k] = { value: value == null ? "" : value, formula: formula || null, style: style || null };
    }
  };

  // ---- grid (DOM) --------------------------------------------------------
  function SheetGrid(model, opts) {
    this.model = model;
    this.wrap = document.getElementById("sheet-grid");
    this.canEdit = !!opts.canEdit;
    this.onCommit = opts.onCommit;      // (cellsArray) -> void
    this.onSelChange = opts.onSelChange; // (r,c,sel) -> void
    this.active = { r: 0, c: 0 };
    this.sel = { r1: 0, c1: 0, r2: 0, c2: 0 }; // range selezione
    this.editor = null;
    this.remoteCursors = {}; // uid -> {r,c,el,color,email}
    this._build();
    this._bind();
    this.setActive(0, 0);
  }

  SheetGrid.prototype._build = function () {
    var m = this.model;
    var html = ['<table class="sheet-table"><thead><tr><th class="corner"></th>'];
    for (var c = 0; c < m.nCols; c++) html.push('<th data-c="' + c + '">' + colLabel(c) + "</th>");
    html.push("</tr></thead><tbody>");
    for (var r = 0; r < m.nRows; r++) {
      html.push('<tr><th class="rowhead" data-r="' + r + '">' + (r + 1) + "</th>");
      for (var c2 = 0; c2 < m.nCols; c2++) {
        html.push('<td class="cell" data-r="' + r + '" data-c="' + c2 + '"></td>');
      }
      html.push("</tr>");
    }
    html.push("</tbody></table>");
    this.wrap.innerHTML = html.join("");
    this.table = this.wrap.querySelector("table");
    // index celle per accesso O(1)
    this._tds = {};
    var tds = this.table.querySelectorAll("td.cell");
    for (var i = 0; i < tds.length; i++) {
      this._tds[key(+tds[i].dataset.r, +tds[i].dataset.c)] = tds[i];
    }
  };

  SheetGrid.prototype.td = function (r, c) { return this._tds[key(r, c)]; };

  SheetGrid.prototype.renderCell = function (r, c) {
    var td = this.td(r, c);
    if (!td) return;
    var cell = this.model.get(r, c);
    td.textContent = cell ? (cell.value || "") : "";
  };

  SheetGrid.prototype.renderAll = function () {
    for (var k in this.model.cells) {
      var parts = k.split(","); this.renderCell(+parts[0], +parts[1]);
    }
  };

  SheetGrid.prototype._clearSelClasses = function () {
    var sel = this.table.querySelectorAll(".in-range, .is-active, .head-active");
    for (var i = 0; i < sel.length; i++) sel[i].classList.remove("in-range", "is-active", "head-active");
  };

  SheetGrid.prototype._paintSelection = function () {
    this._clearSelClasses();
    var s = this.sel;
    var r1 = Math.min(s.r1, s.r2), r2 = Math.max(s.r1, s.r2);
    var c1 = Math.min(s.c1, s.c2), c2 = Math.max(s.c1, s.c2);
    for (var r = r1; r <= r2; r++) {
      for (var c = c1; c <= c2; c++) {
        var td = this.td(r, c);
        if (td) td.classList.add("in-range");
      }
    }
    var act = this.td(this.active.r, this.active.c);
    if (act) act.classList.add("is-active");
    // header highlight
    var chs = this.table.querySelectorAll('thead th[data-c]');
    for (var i = 0; i < chs.length; i++) {
      var cc = +chs[i].dataset.c;
      if (cc >= c1 && cc <= c2) chs[i].classList.add("head-active");
    }
    var rhs = this.table.querySelectorAll('tbody th.rowhead');
    for (var j = 0; j < rhs.length; j++) {
      var rr = +rhs[j].dataset.r;
      if (rr >= r1 && rr <= r2) rhs[j].classList.add("head-active");
    }
  };

  SheetGrid.prototype.setActive = function (r, c, extend) {
    r = clamp(r, 0, this.model.nRows - 1);
    c = clamp(c, 0, this.model.nCols - 1);
    this.active = { r: r, c: c };
    if (extend) {
      this.sel.r2 = r; this.sel.c2 = c;
    } else {
      this.sel = { r1: r, c1: c, r2: r, c2: c };
    }
    this._paintSelection();
    this._scrollIntoView(r, c);
    if (this.onSelChange) this.onSelChange(r, c, this.sel);
  };

  SheetGrid.prototype._scrollIntoView = function (r, c) {
    var td = this.td(r, c);
    if (!td) return;
    var wr = this.wrap.getBoundingClientRect();
    var cr = td.getBoundingClientRect();
    // tieni conto delle intestazioni sticky (~ row height / rowhead width)
    var padTop = 26, padLeft = 46;
    if (cr.top < wr.top + padTop) this.wrap.scrollTop -= (wr.top + padTop - cr.top);
    else if (cr.bottom > wr.bottom) this.wrap.scrollTop += (cr.bottom - wr.bottom);
    if (cr.left < wr.left + padLeft) this.wrap.scrollLeft -= (wr.left + padLeft - cr.left);
    else if (cr.right > wr.right) this.wrap.scrollLeft += (cr.right - wr.right);
  };

  // ---- editing -----------------------------------------------------------
  SheetGrid.prototype.startEdit = function (initial) {
    if (!this.canEdit) return;
    var r = this.active.r, c = this.active.c;
    var td = this.td(r, c);
    if (!td) return;
    if (this.editor) this.commitEdit();
    var ed = document.createElement("input");
    ed.className = "sheet-cell-editor";
    ed.type = "text";
    var cell = this.model.get(r, c);
    ed.value = initial != null ? initial : (cell ? (cell.value || "") : "");
    this.wrap.appendChild(ed);
    this._positionEditor(ed, td);
    this.editor = { el: ed, r: r, c: c };
    ed.focus();
    if (initial != null) ed.setSelectionRange(ed.value.length, ed.value.length);
    else ed.select();
    var self = this;
    ed.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); self.commitEdit(); self.setActive(r + 1, c); self.wrap.focus(); }
      else if (e.key === "Tab") { e.preventDefault(); self.commitEdit(); self.setActive(r, c + (e.shiftKey ? -1 : 1)); self.wrap.focus(); }
      else if (e.key === "Escape") { e.preventDefault(); self.cancelEdit(); self.wrap.focus(); }
      e.stopPropagation();
    });
  };

  SheetGrid.prototype._positionEditor = function (ed, td) {
    var wr = this.wrap.getBoundingClientRect();
    var cr = td.getBoundingClientRect();
    ed.style.left = (cr.left - wr.left + this.wrap.scrollLeft) + "px";
    ed.style.top = (cr.top - wr.top + this.wrap.scrollTop) + "px";
    ed.style.width = Math.max(cr.width, 80) + "px";
    ed.style.minHeight = cr.height + "px";
  };

  SheetGrid.prototype.commitEdit = function () {
    if (!this.editor) return;
    var e = this.editor, val = e.el.value;
    this.editor = null;
    e.el.remove();
    var prev = this.model.get(e.r, e.c);
    var prevVal = prev ? (prev.value || "") : "";
    if (val !== prevVal) {
      this.model.setCell(e.r, e.c, val, null, null);
      this.renderCell(e.r, e.c);
      if (this.onCommit) this.onCommit([{ row: e.r, col: e.c, value: val, formula: null }]);
    }
  };

  SheetGrid.prototype.cancelEdit = function () {
    if (!this.editor) return;
    this.editor.el.remove();
    this.editor = null;
  };

  // cancella il contenuto della selezione corrente
  SheetGrid.prototype.clearSelection = function () {
    if (!this.canEdit) return;
    var s = this.sel, patch = [];
    var r1 = Math.min(s.r1, s.r2), r2 = Math.max(s.r1, s.r2);
    var c1 = Math.min(s.c1, s.c2), c2 = Math.max(s.c1, s.c2);
    for (var r = r1; r <= r2; r++)
      for (var c = c1; c <= c2; c++)
        if (this.model.get(r, c)) {
          this.model.setCell(r, c, "", null, null);
          this.renderCell(r, c);
          patch.push({ row: r, col: c, value: "", formula: null });
        }
    if (patch.length && this.onCommit) this.onCommit(patch);
  };

  // ---- copia / incolla ---------------------------------------------------
  SheetGrid.prototype.selectionTSV = function () {
    var s = this.sel;
    var r1 = Math.min(s.r1, s.r2), r2 = Math.max(s.r1, s.r2);
    var c1 = Math.min(s.c1, s.c2), c2 = Math.max(s.c1, s.c2);
    var rows = [];
    for (var r = r1; r <= r2; r++) {
      var line = [];
      for (var c = c1; c <= c2; c++) {
        var cell = this.model.get(r, c);
        line.push(cell ? (cell.value || "") : "");
      }
      rows.push(line.join("\t"));
    }
    return rows.join("\n");
  };

  SheetGrid.prototype.pasteTSV = function (text) {
    if (!this.canEdit || !text) return;
    var rows = text.replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n");
    if (rows.length && rows[rows.length - 1] === "") rows.pop();
    var r0 = this.active.r, c0 = this.active.c, patch = [];
    for (var i = 0; i < rows.length; i++) {
      var cols = rows[i].split("\t");
      for (var j = 0; j < cols.length; j++) {
        var r = r0 + i, c = c0 + j;
        if (r >= this.model.nRows || c >= this.model.nCols) continue;
        this.model.setCell(r, c, cols[j], null, null);
        this.renderCell(r, c);
        patch.push({ row: r, col: c, value: cols[j], formula: null });
      }
    }
    if (patch.length && this.onCommit) this.onCommit(patch);
  };

  // ---- patch remote (altri utenti) --------------------------------------
  SheetGrid.prototype.applyRemoteCells = function (cells) {
    for (var i = 0; i < cells.length; i++) {
      var c = cells[i];
      this.model.setCell(c.row, c.col, c.value, c.formula, c.style);
      this.renderCell(c.row, c.col);
    }
  };

  // ---- cursori remoti ----------------------------------------------------
  SheetGrid.prototype.setRemoteCursor = function (uid, email, r, c) {
    if (uid === cfg.user_id) return;
    var cur = this.remoteCursors[uid];
    var color = colorFor(uid);
    if (cur && cur.td) cur.td.classList.remove("remote-sel");
    if (cur && cur.flag) cur.flag.remove();
    var td = this.td(r, c);
    if (!td) { delete this.remoteCursors[uid]; return; }
    td.style.setProperty("--remote-color", color);
    td.classList.add("remote-sel");
    var flag = document.createElement("div");
    flag.className = "sheet-cursor-flag";
    flag.style.background = color;
    flag.textContent = (email || "?").split("@")[0];
    var wr = this.wrap.getBoundingClientRect(), cr = td.getBoundingClientRect();
    flag.style.left = (cr.left - wr.left + this.wrap.scrollLeft) + "px";
    flag.style.top = (cr.top - wr.top + this.wrap.scrollTop) + "px";
    this.wrap.appendChild(flag);
    this.remoteCursors[uid] = { td: td, flag: flag, color: color, email: email };
  };
  SheetGrid.prototype.dropRemoteCursor = function (uid) {
    var cur = this.remoteCursors[uid];
    if (!cur) return;
    if (cur.td) cur.td.classList.remove("remote-sel");
    if (cur.flag) cur.flag.remove();
    delete this.remoteCursors[uid];
  };

  // ---- eventi ------------------------------------------------------------
  SheetGrid.prototype._cellFromEvent = function (e) {
    var td = e.target.closest && e.target.closest("td.cell");
    if (!td) return null;
    return { r: +td.dataset.r, c: +td.dataset.c };
  };

  SheetGrid.prototype._bind = function () {
    var self = this;
    var dragging = false;

    this.wrap.addEventListener("mousedown", function (e) {
      var p = self._cellFromEvent(e);
      if (!p) return;
      if (self.editor) self.commitEdit();
      self.setActive(p.r, p.c, e.shiftKey);
      dragging = true;
      self.wrap.focus();
    });
    this.wrap.addEventListener("mousemove", function (e) {
      if (!dragging) return;
      var p = self._cellFromEvent(e);
      if (p) self.setActive(p.r, p.c, true);
    });
    document.addEventListener("mouseup", function () { dragging = false; });

    this.wrap.addEventListener("dblclick", function (e) {
      var p = self._cellFromEvent(e);
      if (p) { self.setActive(p.r, p.c); self.startEdit(); }
    });

    this.wrap.addEventListener("scroll", function () {
      if (self.editor) self._positionEditor(self.editor.el, self.td(self.editor.r, self.editor.c));
      // riposiziona i flag dei cursori remoti
      for (var uid in self.remoteCursors) {
        var cur = self.remoteCursors[uid];
        if (cur.td && cur.flag) {
          var wr = self.wrap.getBoundingClientRect(), cr = cur.td.getBoundingClientRect();
          cur.flag.style.left = (cr.left - wr.left + self.wrap.scrollLeft) + "px";
          cur.flag.style.top = (cr.top - wr.top + self.wrap.scrollTop) + "px";
        }
      }
    });

    this.wrap.addEventListener("keydown", function (e) {
      if (self.editor) return; // l'editor gestisce i suoi tasti
      var a = self.active, handled = true;
      switch (e.key) {
        case "ArrowUp": self.setActive(a.r - 1, a.c, e.shiftKey); break;
        case "ArrowDown": self.setActive(a.r + 1, a.c, e.shiftKey); break;
        case "ArrowLeft": self.setActive(a.r, a.c - 1, e.shiftKey); break;
        case "ArrowRight": self.setActive(a.r, a.c + 1, e.shiftKey); break;
        case "Tab": self.setActive(a.r, a.c + (e.shiftKey ? -1 : 1)); break;
        case "Enter": self.canEdit ? self.startEdit() : self.setActive(a.r + 1, a.c); break;
        case "F2": self.startEdit(); break;
        case "Backspace":
        case "Delete": self.clearSelection(); break;
        case "Home": self.setActive(a.r, 0, e.shiftKey); break;
        case "End": self.setActive(a.r, self.model.nCols - 1, e.shiftKey); break;
        default:
          // carattere stampabile -> inizia edit
          if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
            self.startEdit(e.key);
          } else {
            handled = false;
          }
      }
      if (handled) e.preventDefault();
    });

    // copia / incolla a livello documento (solo quando la griglia ha il focus
    // e non si sta editando una cella).
    document.addEventListener("copy", function (e) {
      if (self.editor || !self.wrap.contains(document.activeElement) && document.activeElement !== self.wrap) return;
      e.clipboardData.setData("text/plain", self.selectionTSV());
      e.preventDefault();
    });
    document.addEventListener("paste", function (e) {
      if (self.editor) return;
      if (!self.wrap.contains(document.activeElement) && document.activeElement !== self.wrap) return;
      var text = (e.clipboardData || window.clipboardData).getData("text");
      self.pasteTSV(text);
      e.preventDefault();
    });
  };

  // =======================================================================
  // Transport: HTTP (Fase 2). Stessa interfaccia di WsTransport (Fase 3).
  // =======================================================================
  function HttpTransport(handlers) { this.h = handlers; this._rev = cfg.revision || 0; }
  HttpTransport.prototype.start = function () {
    var self = this;
    this.h.onStatus("connecting");
    fetch(cfg.snapshot_url, { credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (snap) {
        self._rev = snap.revision;
        self.h.onSnapshot(snap);
        self.h.onStatus("online");
      })
      .catch(function () { self.h.onStatus("offline"); });
  };
  HttpTransport.prototype.sendPatch = function (cells, patchId) {
    var self = this;
    return fetch(cfg.patch_url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ patch_id: patchId, cells: cells })
    }).then(function (r) {
      if (!r.ok) { self.h.onStatus("offline"); throw new Error("patch HTTP " + r.status); }
      return r.json();
    }).then(function (resp) {
      self._rev = resp.revision;
      self.h.onStatus("online");
      self.h.onRevision(resp.revision);
    }).catch(function () { /* gia' segnalato offline */ });
  };
  HttpTransport.prototype.sendCursor = function () { /* no-op su HTTP */ };
  HttpTransport.prototype.lastRevision = function () { return this._rev; };
  HttpTransport.prototype.stop = function () { };

  // =======================================================================
  // App: collega model + grid + transport + UI (formula bar, status, presence)
  // =======================================================================
  function SheetApp() {
    var self = this;
    this.model = new SheetModel(cfg.n_rows, cfg.n_cols);
    this.model.revision = cfg.revision || 0;
    this.statusEl = document.getElementById("sheet-status");
    this.bannerEl = document.getElementById("sheet-conn-banner");
    this.cellrefEl = document.getElementById("sheet-cellref");
    this.formulaEl = document.getElementById("sheet-formula");
    this.presenceEl = document.getElementById("sheet-presence");

    this.grid = new SheetGrid(this.model, {
      canEdit: cfg.can_edit,
      onCommit: function (cells) { self._onLocalCommit(cells); },
      onSelChange: function (r, c) { self._onSelChange(r, c); }
    });

    var handlers = {
      onStatus: function (s) { self.setStatus(s); },
      onSnapshot: function (snap) { self._onSnapshot(snap); },
      onRevision: function (rev) { self.model.revision = rev; },
      onRemotePatch: function (msg) { self._onRemotePatch(msg); },
      onPresence: function (users) { self._onPresence(users); },
      onCursor: function (msg) { self.grid.setRemoteCursor(msg.user_id, msg.email, msg.row, msg.col); },
      onCursorGone: function (uid) { self.grid.dropRemoteCursor(uid); },
      onError: function (msg) { self._onError(msg); }
    };
    // Factory: WsTransport se disponibile (Fase 3), altrimenti HTTP.
    this.transport = (window.ArgosSheetWsTransport && cfg.ws_url)
      ? new window.ArgosSheetWsTransport(cfg, handlers, HttpTransport)
      : new HttpTransport(handlers);

    this._bindFormula();
    this._bindTheme();
    this.transport.start();
  }

  SheetApp.prototype.setStatus = function (state) {
    var labels = { online: "Online", connecting: "Connessione…", reconnecting: "Riconnessione…", offline: "Offline" };
    this.statusEl.dataset.state = state;
    this.statusEl.querySelector(".sheet-status-label").textContent = labels[state] || state;
    // banner prominente quando non siamo connessi
    if (this.bannerEl) {
      if (state === "reconnecting" || state === "offline") {
        this.bannerEl.dataset.state = state;
        this.bannerEl.textContent = state === "offline"
          ? "Connessione persa — le modifiche verranno inviate alla riconnessione."
          : "Riconnessione in corso…";
        this.bannerEl.hidden = false;
      } else {
        this.bannerEl.hidden = true;
      }
    }
  };

  SheetApp.prototype._onSnapshot = function (snap) {
    // reset model e ridisegna
    this.model.cells = {};
    this.model.revision = snap.revision;
    var cells = snap.cells || [];
    for (var i = 0; i < cells.length; i++) {
      var c = cells[i];
      this.model.setCell(c.row, c.col, c.value, c.formula, c.style);
    }
    this.grid.renderAll();
    this._onSelChange(this.grid.active.r, this.grid.active.c);
  };

  SheetApp.prototype._onRemotePatch = function (msg) {
    // ignora l'eco delle nostre patch (gia' applicate ottimisticamente)
    if (msg.patch_id && this._pending && this._pending[msg.patch_id]) {
      delete this._pending[msg.patch_id];
      this.model.revision = msg.revision;
      return;
    }
    if (msg.cells) this.grid.applyRemoteCells(msg.cells);
    this.model.revision = msg.revision;
  };

  SheetApp.prototype._onLocalCommit = function (cells) {
    var patchId = "p" + Date.now() + "_" + Math.floor(Math.random() * 1e6);
    this._pending = this._pending || {};
    this._pending[patchId] = true;
    this.transport.sendPatch(cells, patchId);
    this._onSelChange(this.grid.active.r, this.grid.active.c);
  };

  SheetApp.prototype._onSelChange = function (r, c) {
    this.cellrefEl.textContent = colLabel(c) + (r + 1);
    var cell = this.model.get(r, c);
    if (document.activeElement !== this.formulaEl) {
      this.formulaEl.value = cell ? (cell.value || "") : "";
    }
    this.transport.sendCursor({ row: r, col: c, sel: this.grid.sel });
  };

  SheetApp.prototype._bindFormula = function () {
    var self = this;
    this.formulaEl.addEventListener("keydown", function (e) {
      if (e.key === "Enter") {
        e.preventDefault();
        var r = self.grid.active.r, c = self.grid.active.c, val = self.formulaEl.value;
        var prev = self.model.get(r, c); var prevVal = prev ? (prev.value || "") : "";
        if (val !== prevVal) {
          self.model.setCell(r, c, val, null, null);
          self.grid.renderCell(r, c);
          self._onLocalCommit([{ row: r, col: c, value: val, formula: null }]);
        }
        self.grid.setActive(r + 1, c);
        self.grid.wrap.focus();
      } else if (e.key === "Escape") {
        self._onSelChange(self.grid.active.r, self.grid.active.c);
        self.grid.wrap.focus();
      }
    });
  };

  SheetApp.prototype._bindTheme = function () {
    var btn = document.getElementById("sheet-theme");
    if (!btn) return;
    btn.addEventListener("click", function () {
      var html = document.documentElement;
      var next = html.getAttribute("data-theme") === "light" ? "dark" : "light";
      html.setAttribute("data-theme", next);
      try { localStorage.setItem("argos-theme", next); } catch (e) {}
    });
  };

  SheetApp.prototype._onError = function (msg) {
    if (msg && msg.code === "forbidden") {
      // permesso revocato o sola lettura: blocca l'editing lato client
      this.grid.canEdit = false;
      this.formulaEl.disabled = true;
    }
    if (msg && msg.message) console.warn("[sheet]", msg.code, msg.message);
  };

  SheetApp.prototype._onPresence = function (users) {
    // Costruzione DOM sicura: l'email arriva da WS/DB e NON va concatenata in
    // innerHTML (rischio XSS). textContent/setAttribute neutralizzano l'HTML.
    this.presenceEl.textContent = "";
    var list = users || [];
    for (var i = 0; i < list.length; i++) {
      var u = list[i];
      var span = document.createElement("span");
      span.className = "pavatar";
      span.title = u.email || "";
      span.style.background = u.color || colorFor(u.user_id);
      span.textContent = (u.email || "?").substring(0, 2).toUpperCase();
      this.presenceEl.appendChild(span);
    }
  };

  // =======================================================================
  // Transport: WebSocket (Fase 3+). Stessa interfaccia di HttpTransport.
  // Reconnect con backoff esponenziale + recupero incrementale via hello.
  // =======================================================================
  function WsTransport(config, handlers, HttpCls) {
    this.cfg = config; this.h = handlers; this.HttpCls = HttpCls;
    this._rev = config.revision || 0;
    this._haveSnapshot = false;
    this._ws = null; this._retries = 0; this._closedByUs = false;
    this._pingTimer = null; this._cursorAt = 0;
    this._outbox = [];  // patch fatte mentre offline, da rispedire al reconnect
  }
  WsTransport.prototype._url = function () {
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    return proto + "//" + location.host + this.cfg.ws_url;
  };
  WsTransport.prototype.start = function () {
    var self = this;
    this.h.onStatus(this._retries ? "reconnecting" : "connecting");
    var ws;
    try { ws = new WebSocket(this._url()); }
    catch (e) { this.h.onStatus("offline"); this._scheduleReconnect(); return; }
    this._ws = ws;
    ws.onopen = function () {
      self.h.onStatus("online");
      // hello: snapshot completo se non abbiamo ancora stato, altrimenti incrementale
      ws.send(JSON.stringify({ type: "hello", last_revision: self._haveSnapshot ? self._rev : -1 }));
      // rispedisci le patch accumulate offline (applicate gia' ottimisticamente)
      if (self._outbox.length) {
        var pending = self._outbox; self._outbox = [];
        for (var i = 0; i < pending.length; i++) {
          ws.send(JSON.stringify({ type: "cell_patch", patch_id: pending[i].id, cells: pending[i].cells }));
        }
      }
      self._pingTimer = setInterval(function () {
        if (ws.readyState === 1) ws.send(JSON.stringify({ type: "ping" }));
      }, 25000);
    };
    ws.onmessage = function (ev) {
      var msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
      switch (msg.type) {
        case "snapshot": self._haveSnapshot = true; self._rev = msg.revision; self.h.onSnapshot(msg); self.h.onStatus("online"); break;
        case "revision_patch": self._rev = Math.max(self._rev, msg.revision); self.h.onRemotePatch(msg); break;
        case "sync": self._haveSnapshot = true; self._rev = msg.revision; self.h.onStatus("online"); break;
        case "presence": self.h.onPresence(msg.users); break;
        case "cursor": self.h.onCursor(msg); break;
        case "cursor_gone": if (self.h.onCursorGone) self.h.onCursorGone(msg.user_id); break;
        case "error": if (self.h.onError) self.h.onError(msg); break;
        case "pong": break;
      }
    };
    ws.onclose = function (ev) {
      clearInterval(self._pingTimer);
      if (self._closedByUs) return;
      // auth/forbidden/not-found: non riconnettere
      if (ev.code === 4401 || ev.code === 4403 || ev.code === 4404) {
        self.h.onStatus("offline");
        if (self.h.onError) self.h.onError({ code: ev.code === 4401 ? "unauthorized" : "forbidden",
          message: ev.code === 4401 ? "Sessione scaduta: ricarica la pagina." : "Accesso al foglio negato." });
        return;
      }
      self.h.onStatus("reconnecting");
      self._scheduleReconnect();
    };
    ws.onerror = function () { /* onclose seguira' */ };
  };
  WsTransport.prototype._scheduleReconnect = function () {
    var self = this;
    this._retries = Math.min(this._retries + 1, 10);
    var delay = Math.min(30000, 400 * Math.pow(2, this._retries)); // backoff capped 30s
    setTimeout(function () { if (!self._closedByUs) self.start(); }, delay);
  };
  WsTransport.prototype.sendPatch = function (cells, patchId) {
    if (this._ws && this._ws.readyState === 1) {
      this._ws.send(JSON.stringify({ type: "cell_patch", patch_id: patchId, cells: cells }));
    } else {
      // offline: accoda per il flush al reconnect (cap per evitare crescita illimitata)
      if (this._outbox.length < 2000) this._outbox.push({ id: patchId, cells: cells });
      this.h.onStatus("offline");
    }
    return Promise.resolve();
  };
  WsTransport.prototype.sendCursor = function (payload) {
    if (!this._ws || this._ws.readyState !== 1) return;
    var now = Date.now();
    if (now - this._cursorAt < 120) return; // throttle
    this._cursorAt = now;
    this._ws.send(JSON.stringify({ type: "cursor", row: payload.row, col: payload.col, selection: payload.sel }));
  };
  WsTransport.prototype.lastRevision = function () { return this._rev; };
  WsTransport.prototype.stop = function () {
    this._closedByUs = true; clearInterval(this._pingTimer);
    if (this._ws) try { this._ws.close(); } catch (e) {}
  };
  window.ArgosSheetWsTransport = WsTransport;

  // bootstrap
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { window.argosSheet = new SheetApp(); });
  } else {
    window.argosSheet = new SheetApp();
  }
})();
