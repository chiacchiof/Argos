(function () {
  var phrases = [
    "Argos apre cento occhi sui segnali.",
    "Scruto asset, domini e tracce nel rumore.",
    "Allineo memorie, tenant e piste luminose.",
    "Interrogo il mondo, un lead alla volta.",
    "Rendo visibili pattern nascosti.",
    "Carico mappe, contatti e frammenti.",
    "Metto a fuoco il campo prima della prossima mossa.",
    "L'oracolo di Argos sta leggendo gli indizi."
  ];

  var loader = null;
  var textNode = null;
  var activeRequests = 0;
  var pendingTimer = null;
  var phraseTimer = null;
  var lastPhrase = -1;
  var visible = false;

  function getLoader() {
    if (!loader) {
      loader = document.querySelector('[data-argos-loader]');
      textNode = loader ? loader.querySelector('[data-argos-loader-text]') : null;
    }
    return loader;
  }

  function nextPhrase() {
    if (!textNode || phrases.length === 0) return;
    var idx = Math.floor(Math.random() * phrases.length);
    if (phrases.length > 1 && idx === lastPhrase) {
      idx = (idx + 1) % phrases.length;
    }
    lastPhrase = idx;
    textNode.textContent = phrases[idx];
  }

  function showLoader(delay) {
    activeRequests += 1;
    if (!getLoader() || visible || pendingTimer) return;
    pendingTimer = window.setTimeout(function () {
      pendingTimer = null;
      if (!getLoader() || activeRequests <= 0) return;
      visible = true;
      nextPhrase();
      loader.classList.add('is-visible');
      loader.setAttribute('aria-hidden', 'false');
      phraseTimer = window.setInterval(nextPhrase, 2600);
    }, typeof delay === 'number' ? delay : 140);
  }

  function hideLoader(force) {
    activeRequests = force ? 0 : Math.max(0, activeRequests - 1);
    if (activeRequests > 0) return;
    if (pendingTimer) {
      window.clearTimeout(pendingTimer);
      pendingTimer = null;
    }
    if (phraseTimer) {
      window.clearInterval(phraseTimer);
      phraseTimer = null;
    }
    if (!getLoader() || !visible) return;
    visible = false;
    loader.classList.remove('is-visible');
    loader.setAttribute('aria-hidden', 'true');
  }

  function shouldIgnoreLink(event, link) {
    if (!link || event.defaultPrevented) return true;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return true;
    if (link.target && link.target !== '_self') return true;
    if (link.hasAttribute('download')) return true;
    if (link.dataset.noLoader === 'true') return true;
    if (hasHtmxTrigger(link)) return true;
    var href = link.getAttribute('href');
    if (!href || href === '#') return true;
    try {
      var url = new URL(href, window.location.href);
      if (url.origin !== window.location.origin) return true;
      return url.pathname === window.location.pathname && url.search === window.location.search && !!url.hash;
    } catch (err) {
      return true;
    }
  }

  function closestFromEvent(event, selector) {
    var node = event.target;
    return node && node.closest ? node.closest(selector) : null;
  }

  function hasHtmxTrigger(el) {
    if (!el || !el.matches) return false;
    return !!el.closest('[hx-get], [hx-post], [hx-put], [hx-patch], [hx-delete], [hx-boost], [data-hx-get], [data-hx-post], [data-hx-put], [data-hx-patch], [data-hx-delete], [data-hx-boost]');
  }

  function getHtmxAttr(el, name) {
    var node = el;
    while (node && node.getAttribute) {
      var value = node.getAttribute(name) || node.getAttribute('data-' + name);
      if (value) return value;
      node = node.parentElement;
    }
    return "";
  }

  function shouldIgnoreHtmxRequest(elt) {
    if (!elt || !elt.closest) return false;
    if (elt.closest('[data-no-loader="true"]')) return true;
    var trigger = getHtmxAttr(elt, 'hx-trigger').toLowerCase();
    return /\bevery\s+\d/.test(trigger);
  }

  document.addEventListener('click', function (event) {
    var link = closestFromEvent(event, 'a[href]');
    if (shouldIgnoreLink(event, link)) return;
    showLoader(80);
  });

  document.addEventListener('submit', function (event) {
    var form = closestFromEvent(event, 'form');
    if (!form || event.defaultPrevented || form.dataset.noLoader === 'true') return;
    if (form.target && form.target !== '_self') return;
    if (hasHtmxTrigger(form)) return;
    showLoader(80);
  });

  document.addEventListener('htmx:beforeRequest', function (event) {
    var elt = event.detail && event.detail.elt ? event.detail.elt : event.target;
    if (shouldIgnoreHtmxRequest(elt)) return;
    var delay = elt && elt.closest && elt.closest('.topbar-search') ? 650 : 220;
    showLoader(delay);
  });

  ['htmx:afterSettle', 'htmx:responseError', 'htmx:sendError', 'htmx:timeout', 'htmx:abort'].forEach(function (name) {
    document.addEventListener(name, function () {
      hideLoader(false);
    });
  });

  window.addEventListener('pageshow', function () {
    hideLoader(true);
  });
  window.addEventListener('beforeunload', function () {
    showLoader(0);
  });
})();
