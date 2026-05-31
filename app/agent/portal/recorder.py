"""Recorder live per le macro di compilazione portali.

Apre un browser headed (persistent context = sessione loggata) sul desktop di chi
ospita Argos (single-user su Windows) e permette due operazioni interattive:

- **login**: l'utente fa login a mano sul portale; la sessione persiste nella
  user_data_dir della macro (nessuna password nel DB). Pattern preso dal QR-login
  WhatsApp (app/routes/settings_whatsapp.py).
- **record**: inietta un overlay JS che evidenzia i campi del form al passaggio del
  mouse; a ogni click calcola un selettore robusto (data-testid > aria-label > id >
  name > path CSS) e lo invia a Python via `expose_binding`. I campi catturati si
  accumulano nella macro (db.update_portal_macro), pronti per essere associati alle
  colonne del foglio nella pagina di edit.

`expose_binding` è un pattern NUOVO nel codebase: tutto confinato qui. Il browser è
guidato dall'utente e può chiudersi in qualsiasi momento → ogni await è protetto,
nessuna eccezione deve uccidere il proactor thread.

Stato in-memory per-macro (`_SESSIONS`): valido solo nel processo corrente. Non è
persistito: se Argos riparte, una sessione recorder "aperta" si considera chiusa.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from .form_fill import MacroField, portal_session_dir

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stato delle sessioni recorder attive (per-macro, in-memory)
# ---------------------------------------------------------------------------

@dataclass
class _RecorderSession:
    macro_id: int
    mode: str                      # "login" | "record"
    context: Any = None            # BrowserContext
    playwright: Any = None         # handle async_playwright
    captured: list[dict] = field(default_factory=list)
    stop_event: asyncio.Event | None = None


_SESSIONS: dict[int, _RecorderSession] = {}


def is_active(macro_id: int) -> bool:
    return macro_id in _SESSIONS


def session_mode(macro_id: int) -> str | None:
    s = _SESSIONS.get(macro_id)
    return s.mode if s else None


# ---------------------------------------------------------------------------
# Overlay JS: evidenzia i campi + cattura il selettore al click
# ---------------------------------------------------------------------------

# Calcola un selettore robusto lato pagina (preferenza data-testid>aria>id>name>path)
# e chiama il binding Python `argosCaptureField`. add_init_script lo ri-applica a
# ogni navigazione, così il recorder sopravvive ai cambi pagina del form multi-step.
_OVERLAY_JS = r"""
() => {
  if (window.__argosRecorderInstalled) return;
  window.__argosRecorderInstalled = true;

  // Fase corrente del recorder (warmup | activity | return | closing).
  window.__argosPhase = window.__argosPhase || 'activity';

  const css = document.createElement('style');
  css.textContent = `
    .__argos-hi { outline: 2px solid #2da3ff !important; outline-offset: 1px !important;
                  background: rgba(45,163,255,.08) !important; cursor: crosshair !important; }
    .__argos-hi-submit { outline: 2px solid #34c759 !important; outline-offset: 1px !important;
                  background: rgba(52,199,89,.12) !important; cursor: crosshair !important; }
    .__argos-hi-nav { outline: 2px dashed #ff9f0a !important; outline-offset: 1px !important;
                  background: rgba(255,159,10,.10) !important; cursor: crosshair !important; }
    #__argos-bar { position: fixed; z-index: 2147483647; right: 12px; bottom: 56px;
                   background: #11151c; color: #e8f0fa; font: 13px/1.3 system-ui, sans-serif;
                   padding: 8px 10px; border: 1px solid #2da3ff; border-radius: 8px;
                   box-shadow: 0 4px 18px rgba(0,0,0,.5); opacity: .55; transition: opacity .15s; }
    #__argos-bar:hover { opacity: 1; }
    #__argos-bar b { display:block; margin-bottom:6px; font-size:12px; opacity:.85; }
    #__argos-bar button { font: 12px system-ui; margin: 0 3px 0 0; padding: 4px 8px;
                   border: 1px solid #2f3947; background:#1b212b; color:#cdd7e3;
                   border-radius: 6px; cursor: pointer; }
    #__argos-bar button.active { background:#2da3ff; color:#03121f; border-color:#2da3ff; font-weight:600; }
    #__argos-toast { position: fixed; z-index: 2147483647; left: 12px; bottom: 12px;
                     background: #11151c; color: #e8f0fa; font: 13px/1.4 system-ui, sans-serif;
                     padding: 8px 12px; border: 1px solid #2da3ff; border-radius: 6px;
                     max-width: 340px; box-shadow: 0 4px 18px rgba(0,0,0,.4); }
  `;
  document.documentElement.appendChild(css);

  // Barra di selezione fase: l'utente sceglie in QUALE fase sta registrando.
  const bar = document.createElement('div');
  bar.id = '__argos-bar';
  const PHASES = [
    ['warmup', '1·Avvio (warmup)'], ['activity', '2·Compilazione'],
    ['return', '3·Ritorno'], ['closing', '4·Chiusura'],
  ];
  bar.innerHTML = '<b>Recorder Argos — fase corrente</b>';
  PHASES.forEach(([key, label]) => {
    const b = document.createElement('button');
    b.textContent = label; b.dataset.phase = key;
    if (key === window.__argosPhase) b.classList.add('active');
    b.addEventListener('click', (ev) => {
      ev.preventDefault(); ev.stopPropagation();
      window.__argosPhase = key;
      bar.querySelectorAll('button').forEach(x => x.classList.toggle('active', x.dataset.phase === key));
      toast.textContent = 'Fase: ' + label + ' — registra le azioni di questa fase.';
    }, true);
    bar.appendChild(b);
  });
  document.documentElement.appendChild(bar);

  const toast = document.createElement('div');
  toast.id = '__argos-toast';
  toast.textContent = 'Scegli la fase in alto, poi clicca i campi / i link da registrare.';
  document.documentElement.appendChild(toast);

  function inBar(el) { return el && el.closest && el.closest('#__argos-bar'); }

  function isSubmit(el) {
    // Il bottone che INVIA un form: input[type=submit] o button che fa submit.
    // Un <button> senza type esplicito invia SOLO se è dentro un <form>; fuori
    // da un form (es. <button onclick="apriForm()">) è navigazione, non submit.
    if (!el || !el.tagName) return false;
    const t = el.tagName.toLowerCase();
    if (t === 'button') {
      const ty = (el.getAttribute('type') || '').toLowerCase();
      if (ty === 'submit') return true;
      if (ty === 'button' || ty === 'reset') return false;
      return !!el.closest('form');  // type assente: submit solo dentro un form
    }
    if (t === 'input') {
      return ['submit','image'].includes((el.getAttribute('type') || '').toLowerCase());
    }
    return false;
  }

  function isField(el) {
    if (!el || !el.tagName) return false;
    const t = el.tagName.toLowerCase();
    if (t === 'textarea' || t === 'select') return true;
    if (t === 'input') {
      const ty = (el.getAttribute('type') || 'text').toLowerCase();
      return !['hidden','submit','button','image','reset'].includes(ty);
    }
    return false;
  }

  function isNav(el) {
    // Click di navigazione: link, button[type=button], elementi role=button/link,
    // voci di menu. Escluso ciò che è già field o submit.
    if (!el || !el.tagName || isField(el) || isSubmit(el)) return false;
    const t = el.tagName.toLowerCase();
    if (t === 'a' || t === 'button') return true;
    const role = (el.getAttribute('role') || '').toLowerCase();
    if (['button', 'link', 'menuitem', 'tab'].includes(role)) return true;
    return false;
  }

  function navTarget(el) {
    // Risale al primo antenato azionabile (es. click su <span> dentro un <a>).
    let node = el;
    for (let i = 0; node && i < 4; i++) {
      if (isNav(node) || isField(node) || isSubmit(node)) return node;
      node = node.parentElement;
    }
    return el;
  }

  function isCapturable(el) { return isField(el) || isSubmit(el) || isNav(el); }

  function cssPath(el) {
    // Fallback robusto: catena di nth-of-type fino a un id o alla radice (max 5 livelli).
    const parts = [];
    let node = el;
    for (let depth = 0; node && node.nodeType === 1 && depth < 5; depth++) {
      if (node.id) { parts.unshift('#' + CSS.escape(node.id)); break; }
      let sel = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const sibs = Array.from(parent.children).filter(c => c.tagName === node.tagName);
        if (sibs.length > 1) sel += `:nth-of-type(${sibs.indexOf(node) + 1})`;
      }
      parts.unshift(sel);
      node = node.parentElement;
    }
    return parts.join(' > ');
  }

  function robustSelector(el) {
    // Preferenza: data-testid > aria-label > id > name > cssPath
    const testid = el.getAttribute('data-testid');
    if (testid) return { selector: `[data-testid="${testid}"]`, strategy: 'css' };
    const aria = el.getAttribute('aria-label');
    if (aria) return { selector: `[aria-label="${aria}"]`, strategy: 'css' };
    if (el.id) return { selector: '#' + CSS.escape(el.id), strategy: 'css' };
    const name = el.getAttribute('name');
    if (name) return { selector: `${el.tagName.toLowerCase()}[name="${name}"]`, strategy: 'css' };
    return { selector: cssPath(el), strategy: 'css' };
  }

  function fieldLabel(el) {
    if (isSubmit(el) || isNav(el)) {
      // Per un bottone/link, l'etichetta è il suo testo (o value/aria/title).
      return (el.textContent || el.getAttribute('value') || el.getAttribute('aria-label')
              || el.getAttribute('title') || 'Azione').trim().slice(0, 80);
    }
    if (el.id) {
      const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (lab && lab.textContent.trim()) return lab.textContent.trim().slice(0, 80);
    }
    const wrap = el.closest('label');
    if (wrap && wrap.textContent.trim()) return wrap.textContent.trim().slice(0, 80);
    return (el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('name') || '').slice(0, 80);
  }

  let last = null;
  document.addEventListener('mouseover', (e) => {
    if (inBar(e.target)) return;
    if (last) { last.classList.remove('__argos-hi','__argos-hi-submit','__argos-hi-nav'); }
    const t = navTarget(e.target);
    if (isSubmit(t)) { t.classList.add('__argos-hi-submit'); last = t; }
    else if (isField(t)) { t.classList.add('__argos-hi'); last = t; }
    else if (isNav(t)) { t.classList.add('__argos-hi-nav'); last = t; }
  }, true);

  document.addEventListener('click', (e) => {
    if (inBar(e.target)) return;  // i pulsanti fase hanno il loro handler
    const el = navTarget(e.target);
    if (!isCapturable(el)) return;

    const submit = isSubmit(el);
    const nav = isNav(el);
    const field = isField(el);

    // RECORDER PASSIVO: cattura il selettore ma LASCIA PROCEDERE l'azione, così
    // l'utente può navigare il percorso reale (entra in area, salva, torna,
    // esci) e registrarlo passo-passo. Bloccare i click romperebbe i flussi
    // multi-pagina (es. "Nuovo" non aprirebbe il form). Dopo una vera
    // navigazione, l'overlay viene re-iniettato da add_init_script.
    // NB: niente preventDefault/stopPropagation.

    const rs = robustSelector(el);
    const action = submit ? 'submit'
        : nav ? 'click'
        : (el.tagName.toLowerCase() === 'input' &&
           ['checkbox','radio'].includes((el.getAttribute('type')||'').toLowerCase())) ? 'click' : 'fill';
    const payload = {
      selector: rs.selector,
      strategy: rs.strategy,
      label: fieldLabel(el),
      tag: el.tagName.toLowerCase(),
      type: (el.getAttribute('type') || el.tagName).toLowerCase(),
      action: action,
      phase: window.__argosPhase || 'activity',
    };
    if (window.argosCaptureField) {
      window.argosCaptureField(payload).then((msg) => {
        toast.textContent = msg || ('Registrato: ' + payload.label);
      }).catch(() => {});
    }
  }, true);
}
"""


# ---------------------------------------------------------------------------
# Apertura browser headed persistent (riusa il pattern di runner_portal_fill)
# ---------------------------------------------------------------------------

async def _open_headed(user_data_dir) -> tuple[Any, Any, Any]:
    try:
        from patchright.async_api import async_playwright as _ap
    except ImportError:
        from playwright.async_api import async_playwright as _ap
    from pathlib import Path

    Path(user_data_dir).mkdir(parents=True, exist_ok=True)
    p = await _ap().start()
    context = await p.chromium.launch_persistent_context(
        user_data_dir=str(user_data_dir),
        headless=False,  # SEMPRE headed: l'utente interagisce a mano
        viewport={"width": 1280, "height": 900},
        locale="it-IT",
        timezone_id="Europe/Rome",
        args=["--disable-blink-features=AutomationControlled", "--no-default-browser-check"],
    )
    page = context.pages[0] if context.pages else await context.new_page()
    return p, context, page


# ---------------------------------------------------------------------------
# Login on-demand: apre il portale, l'utente fa login, la sessione persiste
# ---------------------------------------------------------------------------

async def run_login(macro_id: int, portal_url: str, session_key: str) -> None:
    """Apre il browser headed sulla home del portale per il login manuale.

    Si chiude quando l'utente chiude la finestra (la pagina emette 'close') o
    quando arriva uno stop esplicito. La sessione persiste nella user_data_dir.
    """
    if macro_id in _SESSIONS:
        log.info("recorder login: sessione gia' attiva per macro %s — skip", macro_id)
        return
    sess = _RecorderSession(macro_id=macro_id, mode="login", stop_event=asyncio.Event())
    _SESSIONS[macro_id] = sess
    try:
        p, context, page = await _open_headed(portal_session_dir(session_key))
        sess.playwright, sess.context = p, context
        try:
            await page.goto(portal_url or "about:blank", wait_until="domcontentloaded", timeout=45_000)
        except Exception as e:
            log.warning("recorder login goto fail: %s", e)
        # Resta aperto finché l'utente non chiude il browser o non arriva stop.
        await _wait_until_closed(context, sess.stop_event)
    except Exception as e:
        log.exception("recorder login failed for macro %s: %s", macro_id, e)
    finally:
        await _teardown(sess)


# ---------------------------------------------------------------------------
# Record on-demand: overlay + expose_binding, cattura i campi cliccati
# ---------------------------------------------------------------------------

async def run_record(macro_id: int, portal_url: str, session_key: str,
                     on_capture) -> None:
    """Apre il browser headed con l'overlay recorder. `on_capture(field_dict)` è
    chiamato (sincrono) per ogni campo cliccato; deve persistere il campo."""
    if macro_id in _SESSIONS:
        log.info("recorder record: sessione gia' attiva per macro %s — skip", macro_id)
        return
    sess = _RecorderSession(macro_id=macro_id, mode="record", stop_event=asyncio.Event())
    _SESSIONS[macro_id] = sess

    async def _binding(source, payload):
        # source = {page, frame, context}; payload = dict dal JS.
        try:
            if not isinstance(payload, dict) or not payload.get("selector"):
                return "ignorato (selettore vuoto)"
            if len(sess.captured) >= 60:
                return "limite 60 campi raggiunto"
            sess.captured.append(payload)
            try:
                on_capture(payload)
            except Exception as e:
                log.warning("recorder on_capture fail: %s", e)
            return f"Campo #{len(sess.captured)} registrato: {payload.get('label') or payload['selector']}"
        except Exception as e:
            log.warning("recorder binding fail: %s", e)
            return "errore"

    try:
        p, context, page = await _open_headed(portal_session_dir(session_key))
        sess.playwright, sess.context = p, context
        try:
            await context.expose_binding("argosCaptureField", _binding)
        except Exception as e:
            log.warning("expose_binding fail (gia' presente?): %s", e)
        await context.add_init_script(f"({_OVERLAY_JS})()")
        try:
            await page.goto(portal_url or "about:blank", wait_until="domcontentloaded", timeout=45_000)
            # add_init_script vale dalle navigazioni successive: inietta anche subito.
            await page.evaluate(f"({_OVERLAY_JS})()")
        except Exception as e:
            log.warning("recorder record goto/inject fail: %s", e)
        await _wait_until_closed(context, sess.stop_event)
    except Exception as e:
        log.exception("recorder record failed for macro %s: %s", macro_id, e)
    finally:
        await _teardown(sess)


def stop(macro_id: int) -> bool:
    """Segnala lo stop a una sessione attiva. True se c'era qualcosa da fermare."""
    sess = _SESSIONS.get(macro_id)
    if not sess:
        return False
    if sess.stop_event:
        try:
            sess.stop_event.set()
        except Exception:
            pass
    return True


# ---------------------------------------------------------------------------
# Helper di vita del browser
# ---------------------------------------------------------------------------

async def _wait_until_closed(context, stop_event: asyncio.Event | None) -> None:
    """Attende che l'utente chiuda il browser O che arrivi uno stop esplicito."""
    closed = asyncio.Event()
    try:
        context.on("close", lambda *_: closed.set())
    except Exception:
        pass
    waiters = [asyncio.create_task(closed.wait())]
    if stop_event is not None:
        waiters.append(asyncio.create_task(stop_event.wait()))
    # Poll di sicurezza: se non ci sono più pagine aperte, considera chiuso.
    async def _poll_pages():
        while True:
            await asyncio.sleep(1.0)
            try:
                if not context.pages:
                    return
            except Exception:
                return
    waiters.append(asyncio.create_task(_poll_pages()))
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waiters:
            w.cancel()


async def _teardown(sess: _RecorderSession) -> None:
    try:
        if sess.context is not None:
            await sess.context.close()
    except Exception:
        pass
    try:
        if sess.playwright is not None:
            await sess.playwright.stop()
    except Exception:
        pass
    _SESSIONS.pop(sess.macro_id, None)


def captured_fields(macro_id: int) -> list[dict]:
    sess = _SESSIONS.get(macro_id)
    return list(sess.captured) if sess else []


def captured_as_macro_fields(macro_id: int) -> list[MacroField]:
    return [MacroField.from_dict(d) for d in captured_fields(macro_id)]
