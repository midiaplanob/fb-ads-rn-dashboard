/**
 * content-isolated.js  —  Roda no contexto ISOLATED (acesso a chrome.*)
 * Recebe eventos do MAIN world e envia ao background service worker.
 */
(function () {
  'use strict';

  window.addEventListener('__rnAdTracker__', (e) => {
    const records = e.detail;
    if (!records?.length) return;
    chrome.runtime.sendMessage({
      type: 'AD_DATA',
      records,
      url: location.href,
      ts: Date.now(),
    }).catch(() => {});
  });

})();
