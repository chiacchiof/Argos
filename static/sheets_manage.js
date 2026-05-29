/* Gestione fogli dalla lista e dal dettaglio fascicolo: menu ⋮ (Modifica/
 * Condividi/Elimina) + modale di condivisione. Condiviso tra sheets_list.html
 * e fascicoli_detail.html (incluso via _sheet_manage_assets.html). */
(function () {
  "use strict";

  function modal() { return document.getElementById("sheet-modal"); }
  function body() { return document.getElementById("sheet-share-body"); }

  window.argosCloseShareModal = function () { var m = modal(); if (m) m.hidden = true; };

  window.argosShareOpen = function (id) {
    fetch("/sheets/" + id + "/share", { credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); })
      .then(function (html) { body().innerHTML = html; modal().hidden = false; })
      .catch(function () { alert("Impossibile aprire la condivisione."); });
  };

  window.argosShareApply = function (form) {
    fetch(form.action, { method: "POST", credentials: "same-origin", body: new FormData(form) })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); })
      .then(function (html) { body().innerHTML = html; })
      .catch(function () { alert("Modifica non riuscita."); });
  };

  window.argosRenameSheet = function (id, current) {
    var t = prompt("Nuovo nome del foglio:", current || "");
    if (t === null) return;
    t = t.trim(); if (!t) return;
    var b = new URLSearchParams(); b.set("title", t);
    fetch("/sheets/" + id + "/rename", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: b, redirect: "manual"
    }).then(function () { location.reload(); }).catch(function () { location.reload(); });
  };

  window.argosDeleteSheet = function (id, title) {
    if (!confirm("Eliminare definitivamente «" + (title || "questo foglio") + "»? Celle e cronologia verranno perse.")) return;
    fetch("/sheets/" + id + "/delete", { method: "POST", credentials: "same-origin", redirect: "manual" })
      .then(function () { location.reload(); }).catch(function () { location.reload(); });
  };

  // handler delegato per i bottoni del menu + chiusura cliccando fuori
  document.addEventListener("click", function (e) {
    var btn = e.target.closest(".sheet-kebab-menu button");
    if (btn) {
      var id = btn.dataset.id;
      if (btn.classList.contains("kebab-rename")) window.argosRenameSheet(id, btn.dataset.title);
      else if (btn.classList.contains("kebab-share")) window.argosShareOpen(id);
      else if (btn.classList.contains("kebab-del")) window.argosDeleteSheet(id, btn.dataset.title);
      var d = btn.closest("details.sheet-kebab"); if (d) d.removeAttribute("open");
      return;
    }
    document.querySelectorAll("details.sheet-kebab[open]").forEach(function (d) {
      if (!d.contains(e.target)) d.removeAttribute("open");
    });
  });
})();
