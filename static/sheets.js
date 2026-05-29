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

  var SHEETS_JS_VERSION = "v4-selezione+ref-highlight-2026-05-29";
  try { console.info("[Argos Fogli] sheets.js", SHEETS_JS_VERSION, "caricato"); } catch (e) {}

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
  // Una cella e' una formula se inizia con "=" oppure "+" (abitudine Excel/Lotus).
  // "+A1" viene normalizzato a "=+A1" (l'engine valuta il "+" unario).
  function isFormulaText(v) { return !!v && (v.charAt(0) === "=" || v.charAt(0) === "+"); }
  function normalizeFormula(val) {
    if (!val) return { raw: val, formula: null };
    if (val.charAt(0) === "=") return { raw: val, formula: val };
    if (val.charAt(0) === "+") return { raw: val, formula: "=" + val };
    return { raw: val, formula: null };
  }
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

  // ---- motore formule ----------------------------------------------------
  // Una cella e' una formula se il testo inizia con "=". Conserviamo `formula`
  // (testo grezzo "=A1+SUM(B1:B3)") e `value` (risultato calcolato, mostrato in
  // griglia ed esportato). Il client che EDITA ricalcola tutte le formule e
  // diffonde i valori calcolati; i client che ricevono applicano i valori.
  function FormulaEngine(model) { this.model = model; }
  function colToIndex(letters) {
    var n = 0;
    for (var i = 0; i < letters.length; i++) n = n * 26 + (letters.charCodeAt(i) - 64);
    return n - 1;
  }
  FormulaEngine.prototype.recomputeAll = function () {
    var changed = [], memo = {};
    for (var k in this.model.cells) {
      var cell = this.model.cells[k];
      if (cell.formula) {
        var p = k.split(","), r = +p[0], c = +p[1];
        var val = this._evalCell(r, c, {}, memo);
        if (val !== cell.value) { cell.value = val; changed.push({ r: r, c: c }); }
      }
    }
    return changed;
  };
  FormulaEngine.prototype._numeric = function (r, c, visiting, memo) {
    var cell = this.model.get(r, c);
    if (!cell) return 0;
    var raw = cell.formula ? this._evalCell(r, c, visiting, memo) : cell.value;
    // Propaga gli errori (#CYCLE/#ERR/#NAME) invece di trattarli come 0: cosi'
    // una formula che referenzia una cella in errore diventa anch'essa errore.
    if (typeof raw === "string" && raw.charAt(0) === "#") throw new Error(raw);
    var n = parseFloat(raw);
    return isNaN(n) ? 0 : n;  // testo non numerico = 0 (come gli spreadsheet)
  };
  FormulaEngine.prototype._evalCell = function (r, c, visiting, memo) {
    var key = r + "," + c;
    if (memo[key] !== undefined) return memo[key];
    if (visiting[key]) return "#CYCLE";
    var cell = this.model.get(r, c);
    if (!cell || !cell.formula) { var v = cell ? cell.value : ""; memo[key] = v; return v; }
    visiting[key] = true;
    var result;
    try {
      var n = this._evalExpr(this._tokenize(cell.formula.slice(1)), { i: 0 }, visiting, memo);
      result = (typeof n === "number" && isFinite(n)) ? String(Math.round(n * 1e10) / 1e10) : "#ERR";
    } catch (e) { result = (e && e.message === "#CYCLE") ? "#CYCLE" : "#ERR"; }
    delete visiting[key];
    memo[key] = result;
    return result;
  };
  FormulaEngine.prototype._tokenize = function (s) {
    var re = /\s+|([A-Za-z]+\d+:[A-Za-z]+\d+)|([A-Za-z]+\d+)|([A-Za-z]+)|(\d+\.?\d*)|([+\-*/(),])/g, toks = [], m;
    while ((m = re.exec(s)) !== null) {
      if (m[0].trim() === "" && !m[1] && !m[2] && !m[3] && !m[4] && !m[5]) continue;
      if (m[1]) toks.push({ t: "range", v: m[1] });
      else if (m[2]) toks.push({ t: "cell", v: m[2] });
      else if (m[3]) toks.push({ t: "func", v: m[3].toUpperCase() });
      else if (m[4]) toks.push({ t: "num", v: parseFloat(m[4]) });
      else if (m[5]) toks.push({ t: "op", v: m[5] });
    }
    return toks;
  };
  FormulaEngine.prototype._refToRC = function (ref) {
    var m = /^([A-Za-z]+)(\d+)$/.exec(ref);
    return { r: parseInt(m[2], 10) - 1, c: colToIndex(m[1].toUpperCase()) };
  };
  FormulaEngine.prototype._rangeValues = function (range, visiting, memo) {
    var parts = range.split(":"), a = this._refToRC(parts[0]), b = this._refToRC(parts[1]);
    var r1 = Math.min(a.r, b.r), r2 = Math.max(a.r, b.r), c1 = Math.min(a.c, b.c), c2 = Math.max(a.c, b.c), out = [];
    for (var r = r1; r <= r2; r++) for (var c = c1; c <= c2; c++) out.push(this._numeric(r, c, visiting, memo));
    return out;
  };
  FormulaEngine.prototype._evalExpr = function (toks, pos, visiting, memo) {
    var v = this._evalTerm(toks, pos, visiting, memo);
    while (pos.i < toks.length && toks[pos.i].t === "op" && (toks[pos.i].v === "+" || toks[pos.i].v === "-")) {
      var op = toks[pos.i++].v, rhs = this._evalTerm(toks, pos, visiting, memo);
      v = op === "+" ? v + rhs : v - rhs;
    }
    return v;
  };
  FormulaEngine.prototype._evalTerm = function (toks, pos, visiting, memo) {
    var v = this._evalFactor(toks, pos, visiting, memo);
    while (pos.i < toks.length && toks[pos.i].t === "op" && (toks[pos.i].v === "*" || toks[pos.i].v === "/")) {
      var op = toks[pos.i++].v, rhs = this._evalFactor(toks, pos, visiting, memo);
      v = op === "*" ? v * rhs : v / rhs;
    }
    return v;
  };
  FormulaEngine.prototype._evalFactor = function (toks, pos, visiting, memo) {
    var tk = toks[pos.i];
    if (!tk) throw new Error("#ERR");
    if (tk.t === "op" && tk.v === "-") { pos.i++; return -this._evalFactor(toks, pos, visiting, memo); }
    if (tk.t === "op" && tk.v === "+") { pos.i++; return this._evalFactor(toks, pos, visiting, memo); }
    if (tk.t === "num") { pos.i++; return tk.v; }
    if (tk.t === "cell") {
      pos.i++; var rc = this._refToRC(tk.v);
      var val = this._numeric(rc.r, rc.c, visiting, memo);
      return val;
    }
    if (tk.t === "op" && tk.v === "(") {
      pos.i++; var e = this._evalExpr(toks, pos, visiting, memo);
      if (!toks[pos.i] || toks[pos.i].v !== ")") throw new Error("#ERR");
      pos.i++; return e;
    }
    if (tk.t === "func") {
      pos.i++;
      if (!toks[pos.i] || toks[pos.i].v !== "(") throw new Error("#ERR");
      pos.i++;
      var args = [];
      while (toks[pos.i] && toks[pos.i].v !== ")") {
        if (toks[pos.i].t === "range") { args = args.concat(this._rangeValues(toks[pos.i].v, visiting, memo)); pos.i++; }
        else { args.push(this._evalExpr(toks, pos, visiting, memo)); }
        if (toks[pos.i] && toks[pos.i].v === ",") pos.i++;
      }
      if (!toks[pos.i] || toks[pos.i].v !== ")") throw new Error("#ERR");
      pos.i++;
      return this._applyFunc(tk.v, args);
    }
    throw new Error("#ERR");
  };
  FormulaEngine.prototype._applyFunc = function (name, args) {
    var sum = function (a) { return a.reduce(function (x, y) { return x + y; }, 0); };
    switch (name) {
      case "SUM": return sum(args);
      case "AVERAGE": case "AVG": return args.length ? sum(args) / args.length : 0;
      case "MIN": return args.length ? Math.min.apply(null, args) : 0;
      case "MAX": return args.length ? Math.max.apply(null, args) : 0;
      case "COUNT": return args.length;
      case "PRODUCT": return args.reduce(function (x, y) { return x * y; }, 1);
      case "ABS": return Math.abs(args[0] || 0);
      case "ROUND": return args.length > 1 ? Math.round(args[0] * Math.pow(10, args[1])) / Math.pow(10, args[1]) : Math.round(args[0] || 0);
      default: throw new Error("#NAME");
    }
  };

  // ---- autocomplete funzioni (barra fx / editor cella) ------------------
  var FUNCTIONS = [
    { name: "SUM", sig: "SUM(intervallo)", desc: "Somma dei valori" },
    { name: "AVERAGE", sig: "AVERAGE(intervallo)", desc: "Media dei valori" },
    { name: "MIN", sig: "MIN(intervallo)", desc: "Valore minimo" },
    { name: "MAX", sig: "MAX(intervallo)", desc: "Valore massimo" },
    { name: "COUNT", sig: "COUNT(intervallo)", desc: "Conteggio dei valori" },
    { name: "PRODUCT", sig: "PRODUCT(intervallo)", desc: "Prodotto dei valori" },
    { name: "ABS", sig: "ABS(numero)", desc: "Valore assoluto" },
    { name: "ROUND", sig: "ROUND(numero; cifre)", desc: "Arrotonda a N cifre" }
  ];

  function FnSuggest() {
    this.box = document.createElement("div");
    this.box.className = "sheet-fn-suggest";
    this.box.hidden = true;
    document.body.appendChild(this.box);
    this.items = []; this.sel = 0; this.input = null;
  }
  FnSuggest.prototype.visible = function () { return !this.box.hidden; };
  FnSuggest.prototype.hide = function () { this.box.hidden = true; this.items = []; this.input = null; };
  FnSuggest.prototype._partial = function (input) {
    var caret = input.selectionStart == null ? input.value.length : input.selectionStart;
    var left = input.value.slice(0, caret);
    if (!isFormulaText(left)) return null;            // solo dentro una formula (= o +)
    var m = /([A-Za-z]+)$/.exec(left);
    return m ? m[1] : null;
  };
  FnSuggest.prototype.update = function (input) {
    this.input = input;
    var p = this._partial(input);
    if (!p) { this.hide(); return; }
    var up = p.toUpperCase();
    this.items = FUNCTIONS.filter(function (f) { return f.name.indexOf(up) === 0; });
    if (!this.items.length) { this.hide(); return; }
    this.sel = 0; this._render(); this._position(input); this.box.hidden = false;
  };
  FnSuggest.prototype._render = function () {
    var self = this;
    this.box.textContent = "";
    this.items.forEach(function (f, i) {
      var row = document.createElement("div");
      row.className = "sheet-fn-item" + (i === self.sel ? " is-sel" : "");
      var nm = document.createElement("span"); nm.className = "sheet-fn-name"; nm.textContent = f.sig;
      var ds = document.createElement("span"); ds.className = "sheet-fn-desc"; ds.textContent = f.desc;
      row.appendChild(nm); row.appendChild(ds);
      row.addEventListener("mousedown", function (ev) { ev.preventDefault(); self.sel = i; self.accept(); });
      self.box.appendChild(row);
    });
  };
  FnSuggest.prototype._position = function (input) {
    var r = input.getBoundingClientRect();
    this.box.style.left = r.left + "px";
    this.box.style.top = (r.bottom + 2) + "px";
    this.box.style.minWidth = Math.max(r.width, 240) + "px";
  };
  FnSuggest.prototype.accept = function () {
    if (!this.input || !this.items.length) return;
    var input = this.input, f = this.items[this.sel];
    var caret = input.selectionStart == null ? input.value.length : input.selectionStart;
    var left = input.value.slice(0, caret), right = input.value.slice(caret);
    var newLeft = left.replace(/([A-Za-z]+)$/, f.name + "(");
    input.value = newLeft + right;
    var pos = newLeft.length;
    try { input.setSelectionRange(pos, pos); } catch (e) {}
    this.hide(); input.focus();
  };
  FnSuggest.prototype.onKeydown = function (e) {
    if (!this.visible()) return false;
    if (e.key === "ArrowDown") { this.sel = (this.sel + 1) % this.items.length; this._render(); return true; }
    if (e.key === "ArrowUp") { this.sel = (this.sel - 1 + this.items.length) % this.items.length; this._render(); return true; }
    if (e.key === "Enter" || e.key === "Tab") { this.accept(); return true; }
    if (e.key === "Escape") { this.hide(); return true; }
    return false;
  };

  // ---- grid (DOM) --------------------------------------------------------
  function SheetGrid(model, opts) {
    this.model = model;
    this.wrap = document.getElementById("sheet-grid");
    this.canEdit = !!opts.canEdit;
    this.onCommit = opts.onCommit;      // (cellsArray) -> void
    this.onSelChange = opts.onSelChange; // (r,c,sel) -> void
    this.suggest = opts.suggest || null; // autocomplete funzioni
    this.active = { r: 0, c: 0 };
    this.sel = { r1: 0, c1: 0, r2: 0, c2: 0 }; // range selezione
    this.editor = null;
    this.remoteCursors = {}; // uid -> {r,c,el,color,email}
    this._pointing = null;   // {pos,len} del riferimento inserito col mouse
    this._pointDrag = null;  // {r,c} ancora del drag di selezione range
    this._refCells = [];     // celle evidenziate come riferimenti di formula
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
    // In edit mostra la FORMULA grezza (se presente), non il valore calcolato.
    ed.value = initial != null ? initial : (cell ? (cell.formula || cell.value || "") : "");
    this.wrap.appendChild(ed);
    this._positionEditor(ed, td);
    this.editor = { el: ed, r: r, c: c };
    ed.focus();
    if (initial != null) ed.setSelectionRange(ed.value.length, ed.value.length);
    else ed.select();
    var self = this;
    ed.addEventListener("input", function () {
      self._pointing = null;
      if (self.suggest) self.suggest.update(ed);
      self.highlightFormulaRefs(ed.value);  // aggiorna le celle illuminate
    });
    ed.addEventListener("keydown", function (e) {
      // se il menu funzioni e' aperto, gestisce lui frecce/Invio/Tab/Esc
      if (self.suggest && self.suggest.onKeydown(e)) { e.preventDefault(); e.stopPropagation(); return; }
      if (e.key === "Enter") { e.preventDefault(); self.commitEdit(); self.setActive(r + 1, c); self.wrap.focus(); }
      else if (e.key === "Tab") { e.preventDefault(); self.commitEdit(); self.setActive(r, c + (e.shiftKey ? -1 : 1)); self.wrap.focus(); }
      else if (e.key === "Escape") { e.preventDefault(); self.cancelEdit(); self.wrap.focus(); }
      e.stopPropagation();
    });
    // se si sta gia' digitando una formula (es. ho premuto "="), mostra subito i suggerimenti
    if (self.suggest && ed.value.charAt(0) === "=") self.suggest.update(ed);
    // illumina subito le celle referenziate (caso: apro in edit una cella-formula)
    this.highlightFormulaRefs(ed.value);
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
    if (this.suggest) this.suggest.hide();
    this._pointing = null; this._pointDrag = null;
    this._clearRefHighlights();
    var e = this.editor, val = e.el.value;
    this.editor = null;
    e.el.remove();
    var prev = this.model.get(e.r, e.c);
    var prevRaw = prev ? (prev.formula || prev.value || "") : "";
    if (val !== prevRaw) {
      var norm = normalizeFormula(val);
      var style = prev ? prev.style : null;
      // value temporaneo = testo grezzo; il motore (in SheetApp) calcola il
      // valore reale delle formule e ridisegna.
      this.model.setCell(e.r, e.c, norm.raw, norm.formula, style);
      this.renderCell(e.r, e.c);
      if (this.onCommit) this.onCommit([{ row: e.r, col: e.c, value: norm.raw, formula: norm.formula }]);
    }
  };

  SheetGrid.prototype.cancelEdit = function () {
    if (!this.editor) return;
    if (this.suggest) this.suggest.hide();
    this._pointing = null; this._pointDrag = null;
    this._clearRefHighlights();
    this.editor.el.remove();
    this.editor = null;
  };

  // ---- point-and-click reference (stile Excel/Sheets) -------------------
  // Mentre si edita una formula (= o +), cliccare/trascinare su una cella
  // inserisce il riferimento (A1) o il range (A1:B2) nel punto del cursore.
  SheetGrid.prototype._isFormulaEditor = function () {
    return !!(this.editor && isFormulaText(this.editor.el.value));
  };
  SheetGrid.prototype._refInsertContext = function () {
    if (this._pointing) return true;  // gia' in pointing: i click sostituiscono il ref
    var ed = this.editor.el;
    var caret = ed.selectionStart == null ? ed.value.length : ed.selectionStart;
    var left = ed.value.slice(0, caret);
    return /[=+\-*/(,:]\s*$/.test(left);  // subito dopo = + - * / ( , :
  };
  SheetGrid.prototype._insertRefAtCaret = function (ref, replaceLast) {
    var ed = this.editor.el, val = ed.value;
    var caret = ed.selectionStart == null ? val.length : ed.selectionStart;
    if (replaceLast && this._pointing && caret === this._pointing.pos + this._pointing.len) {
      var before = val.slice(0, this._pointing.pos);
      var after = val.slice(this._pointing.pos + this._pointing.len);
      ed.value = before + ref + after;
      this._pointing.len = ref.length;
    } else {
      var l = val.slice(0, caret), r = val.slice(caret);
      ed.value = l + ref + r;
      this._pointing = { pos: caret, len: ref.length };
    }
    var pos = this._pointing.pos + this._pointing.len;
    try { ed.setSelectionRange(pos, pos); } catch (e) {}
    ed.focus();
    this.highlightFormulaRefs(ed.value);  // il ref appena puntato si illumina
  };

  // ---- evidenziazione celle referenziate da una formula (stile Sheets) --
  var REF_PALETTE = ["#e8710a", "#1a73e8", "#9334e6", "#188038", "#d93025", "#12b5cb", "#e52592", "#f9ab00"];
  SheetGrid.prototype._clearRefHighlights = function () {
    for (var i = 0; i < this._refCells.length; i++) {
      this._refCells[i].classList.remove("ref-hl");
      this._refCells[i].style.removeProperty("--ref-color");
    }
    this._refCells = [];
  };
  SheetGrid.prototype.highlightFormulaRefs = function (text) {
    this._clearRefHighlights();
    var applied = [];
    if (!isFormulaText(text)) return applied;
    function rc(ref) { var m = /^([A-Za-z]+)(\d+)$/.exec(ref); return { r: parseInt(m[2], 10) - 1, c: colToIndex(m[1].toUpperCase()) }; }
    var re = /([A-Za-z]+\d+):([A-Za-z]+\d+)|([A-Za-z]+\d+)/g, m, idx = 0, self = this;
    while ((m = re.exec(text)) !== null) {
      var color = REF_PALETTE[idx % REF_PALETTE.length]; idx++;
      var cells = [];
      if (m[1] && m[2]) {
        var a = rc(m[1]), b = rc(m[2]);
        var r1 = Math.min(a.r, b.r), r2 = Math.max(a.r, b.r), c1 = Math.min(a.c, b.c), c2 = Math.max(a.c, b.c);
        for (var r = r1; r <= r2; r++) for (var c = c1; c <= c2; c++) cells.push([r, c]);
      } else if (m[3]) {
        var x = rc(m[3]); cells.push([x.r, x.c]);
      }
      for (var k = 0; k < cells.length; k++) {
        if (cells[k][0] < 0 || cells[k][1] < 0) continue;
        applied.push({ r: cells[k][0], c: cells[k][1], color: color });
        var td = self.td(cells[k][0], cells[k][1]);
        if (td) { td.classList.add("ref-hl"); td.style.setProperty("--ref-color", color); self._refCells.push(td); }
      }
    }
    return applied;
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
        var norm = normalizeFormula(cols[j]);
        this.model.setCell(r, c, norm.raw, norm.formula, null);
        this.renderCell(r, c);
        patch.push({ row: r, col: c, value: norm.raw, formula: norm.formula });
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
      // POINTING: se sto editando una formula e il caret accetta un riferimento,
      // inserisci A1 nella formula invece di spostare la cella attiva. preventDefault
      // mantiene il focus sull'editor (come in Excel/Sheets).
      if (self._isFormulaEditor() && self._refInsertContext()) {
        e.preventDefault();
        self._insertRefAtCaret(colLabel(p.c) + (p.r + 1), !!self._pointing);
        self._pointDrag = { r: p.r, c: p.c };
        return;
      }
      if (self.editor) self.commitEdit();
      self.setActive(p.r, p.c, e.shiftKey);
      dragging = true;
      self.wrap.focus();
    });
    this.wrap.addEventListener("mousemove", function (e) {
      // drag durante il pointing -> range A1:B2
      if (self._pointDrag && self._isFormulaEditor()) {
        var pp = self._cellFromEvent(e); if (!pp) return;
        var a = self._pointDrag;
        var ref = (a.r === pp.r && a.c === pp.c)
          ? colLabel(pp.c) + (pp.r + 1)
          : colLabel(a.c) + (a.r + 1) + ":" + colLabel(pp.c) + (pp.r + 1);
        self._insertRefAtCaret(ref, true);
        return;
      }
      if (!dragging) return;
      var p = self._cellFromEvent(e);
      if (p) self.setActive(p.r, p.c, true);
    });
    document.addEventListener("mouseup", function () { dragging = false; self._pointDrag = null; });

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
    this.engine = new FormulaEngine(this.model);
    this.suggest = new FnSuggest();
    this.statusEl = document.getElementById("sheet-status");
    this.bannerEl = document.getElementById("sheet-conn-banner");
    this.cellrefEl = document.getElementById("sheet-cellref");
    this.formulaEl = document.getElementById("sheet-formula");
    this.presenceEl = document.getElementById("sheet-presence");

    this.grid = new SheetGrid(this.model, {
      canEdit: cfg.can_edit,
      suggest: this.suggest,
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
    // ricalcola le formule e ridisegna i valori calcolati (display only)
    this._recomputeAndRender();
    this.grid.renderAll();
    this._onSelChange(this.grid.active.r, this.grid.active.c);
  };

  SheetApp.prototype._recomputeAndRender = function () {
    var changed = this.engine.recomputeAll();
    for (var i = 0; i < changed.length; i++) this.grid.renderCell(changed[i].r, changed[i].c);
    return changed;
  };

  SheetApp.prototype._onRemotePatch = function (msg) {
    // ignora l'eco delle nostre patch (gia' applicate ottimisticamente)
    if (msg.patch_id && this._pending && this._pending[msg.patch_id]) {
      delete this._pending[msg.patch_id];
      this.model.revision = msg.revision;
      return;
    }
    if (msg.cells) this.grid.applyRemoteCells(msg.cells);
    this._recomputeAndRender();  // aggiorna eventuali nostre formule dipendenti
    this.model.revision = msg.revision;
    this._onSelChange(this.grid.active.r, this.grid.active.c);
  };

  SheetApp.prototype._onLocalCommit = function (cells) {
    // ricalcola le formule: una modifica puo' cambiare il valore di celle-formula
    // dipendenti, che vanno ridisegnate E persistite (cosi' gli altri client e
    // l'export vedono i valori calcolati). Uniamo celle modificate + dipendenti.
    var changed = this._recomputeAndRender();
    var byKey = {};
    var self = this;
    function put(r, c) {
      var cell = self.model.get(r, c) || {};
      byKey[r + "," + c] = { row: r, col: c, value: cell.value == null ? "" : cell.value, formula: cell.formula || null };
    }
    for (var i = 0; i < cells.length; i++) put(cells[i].row, cells[i].col);
    for (var j = 0; j < changed.length; j++) put(changed[j].r, changed[j].c);
    var payload = Object.keys(byKey).map(function (k) { return byKey[k]; });

    var patchId = "p" + Date.now() + "_" + Math.floor(Math.random() * 1e6);
    this._pending = this._pending || {};
    this._pending[patchId] = true;
    this.transport.sendPatch(payload, patchId);
    this._onSelChange(this.grid.active.r, this.grid.active.c);
  };

  SheetApp.prototype._onSelChange = function (r, c) {
    this.cellrefEl.textContent = colLabel(c) + (r + 1);
    var cell = this.model.get(r, c);
    if (document.activeElement !== this.formulaEl) {
      // mostra la formula grezza se presente, altrimenti il valore
      this.formulaEl.value = cell ? (cell.formula || cell.value || "") : "";
    }
    // Guard: il primo setActive(0,0) parte DENTRO il costruttore di SheetGrid,
    // prima che this.transport/this.grid siano assegnati su SheetApp. Evita il
    // TypeError che bloccava l'intera init (e quindi transport.start()).
    if (this.transport && this.grid) {
      this.transport.sendCursor({ row: r, col: c, sel: this.grid.sel });
    }
  };

  SheetApp.prototype._bindFormula = function () {
    var self = this;
    this.formulaEl.addEventListener("input", function () { self.suggest.update(self.formulaEl); });
    this.formulaEl.addEventListener("blur", function () { setTimeout(function () { self.suggest.hide(); }, 150); });
    this.formulaEl.addEventListener("keydown", function (e) {
      // menu funzioni aperto: gestisce frecce/Invio/Tab/Esc
      if (self.suggest.onKeydown(e)) { e.preventDefault(); return; }
      if (e.key === "Enter") {
        e.preventDefault();
        var r = self.grid.active.r, c = self.grid.active.c, val = self.formulaEl.value;
        var norm = normalizeFormula(val);  // "+A1" -> "=+A1"
        var prev = self.model.get(r, c); var prevRaw = prev ? (prev.formula || prev.value || "") : "";
        if (norm.raw !== prevRaw) {
          self.model.setCell(r, c, norm.raw, norm.formula, prev ? prev.style : null);
          self.grid.renderCell(r, c);
          self._onLocalCommit([{ row: r, col: c, value: norm.raw, formula: norm.formula }]);
        }
        self.suggest.hide();
        self.grid.setActive(r + 1, c);
        self.grid.wrap.focus();
      } else if (e.key === "Escape") {
        self.suggest.hide();
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
  WsTransport.prototype._wsOpen = function () { return this._ws && this._ws.readyState === 1; };

  // Strategia ibrida: carica SUBITO lo snapshot via HTTP (la griglia si popola e
  // il salvataggio funziona anche se il WebSocket non si apre, es. proxy/restart),
  // poi apri il WS per il realtime. Il salvataggio usa il WS se aperto, altrimenti
  // ripiega su HTTP POST. Cosi' la feature funziona sempre; il realtime e' un bonus.
  WsTransport.prototype.start = function () {
    if (!this._haveSnapshot) {
      this.h.onStatus("connecting");
      this._httpSnapshot();
    }
    this._connectWs();
  };
  WsTransport.prototype._httpSnapshot = function () {
    var self = this;
    fetch(self.cfg.snapshot_url, { credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (snap) {
        if (snap.revision >= self._rev) self._rev = snap.revision;
        self._haveSnapshot = true;
        self.h.onSnapshot(snap);
        self.h.onStatus("online");  // dati caricati: si puo' leggere e salvare (via HTTP)
        try { console.info("[Argos Fogli] snapshot HTTP caricato (rev", snap.revision + ")"); } catch (e) {}
      })
      .catch(function (err) {
        try { console.error("[Argos Fogli] snapshot HTTP fallito:", err); } catch (e) {}
        if (!self._wsOpen()) self.h.onStatus("offline");
      });
  };
  WsTransport.prototype._connectWs = function () {
    var self = this;
    var ws;
    try { ws = new WebSocket(this._url()); }
    catch (e) { this._scheduleReconnect(); return; }
    this._ws = ws;
    ws.onopen = function () {
      self._retries = 0;
      self.h.onStatus("online");
      try { console.info("[Argos Fogli] WebSocket connesso (realtime attivo)"); } catch (e) {}
      ws.send(JSON.stringify({ type: "hello", last_revision: self._haveSnapshot ? self._rev : -1 }));
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
      try { console.warn("[Argos Fogli] WebSocket chiuso (code", ev.code + ") — salvataggio via HTTP attivo"); } catch (e) {}
      if (self._closedByUs) return;
      // auth/forbidden/not-found: WS e HTTP falliranno entrambi -> offline + errore.
      if (ev.code === 4401 || ev.code === 4403 || ev.code === 4404) {
        self.h.onStatus("offline");
        if (self.h.onError) self.h.onError({ code: ev.code === 4401 ? "unauthorized" : "forbidden",
          message: ev.code === 4401 ? "Sessione scaduta: ricarica la pagina." : "Accesso al foglio negato." });
        return;
      }
      // realtime perso ma il fallback HTTP salva ancora: NON allarmare l'utente,
      // resta "online" (se abbiamo lo snapshot) e riprova il WS in background.
      if (!self._haveSnapshot) self.h.onStatus("reconnecting");
      self._scheduleReconnect();
    };
    ws.onerror = function () { /* onclose seguira' */ };
  };
  WsTransport.prototype._scheduleReconnect = function () {
    var self = this;
    this._retries = Math.min(this._retries + 1, 10);
    var delay = Math.min(30000, 400 * Math.pow(2, this._retries)); // backoff capped 30s
    setTimeout(function () { if (!self._closedByUs) self._connectWs(); }, delay);
  };
  WsTransport.prototype.sendPatch = function (cells, patchId) {
    // WS se aperto (realtime istantaneo), altrimenti fallback HTTP (salva comunque).
    if (this._wsOpen()) {
      this._ws.send(JSON.stringify({ type: "cell_patch", patch_id: patchId, cells: cells }));
      return Promise.resolve();
    }
    var self = this;
    return fetch(this.cfg.patch_url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ patch_id: patchId, cells: cells })
    }).then(function (r) {
      if (!r.ok) throw new Error("patch HTTP " + r.status);
      return r.json();
    }).then(function (resp) {
      self._rev = Math.max(self._rev, resp.revision);
      self.h.onStatus("online");
      self.h.onRevision(resp.revision);
    }).catch(function () {
      if (self._outbox.length < 2000) self._outbox.push({ id: patchId, cells: cells });
      self.h.onStatus("offline");
    });
  };
  WsTransport.prototype.sendCursor = function (payload) {
    if (!this._wsOpen()) return;
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
  // Esposti per i test (node); innocui nel browser.
  window.ArgosFormulaEngine = FormulaEngine;
  window.ArgosSheetModel = SheetModel;
  window.ArgosFnSuggest = FnSuggest;
  window.ArgosNormalizeFormula = normalizeFormula;

  // bootstrap
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { window.argosSheet = new SheetApp(); });
  } else {
    window.argosSheet = new SheetApp();
  }
})();
