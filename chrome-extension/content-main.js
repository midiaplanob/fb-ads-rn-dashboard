/**
 * content-main.js  —  Roda no contexto MAIN da página
 * =====================================================
 * Intercepta chamadas fetch/XHR do Meta Ad Library e faz scraping do DOM.
 * Sem acesso a APIs do Chrome — comunica via CustomEvent.
 */
(function () {
  'use strict';

  const EVT = '__rnAdTracker__';

  /* ────────────────────────────────────────────────
   * Utilitários
   * ──────────────────────────────────────────────── */

  function cleanFbJson(text) {
    return text.replace(/^for\s*\(;;\);?\s*/, '').trim();
  }

  function dispatch(records) {
    if (!records || records.length === 0) return;
    window.dispatchEvent(new CustomEvent(EVT, { detail: records }));
    console.debug('[RN Ad Tracker] %d registros capturados', records.length);
  }

  /* ────────────────────────────────────────────────
   * Parser de faixa de gasto em BRL
   * Ex: "Menos de R$ 100", "R$ 1.000 – R$ 4.999"
   * ──────────────────────────────────────────────── */

  function parseSpendText(raw) {
    if (!raw) return { lo: 0, hi: 0 };
    const t = String(raw)
      .replace(/ |\./g, '')   // nbsp e pontos de milhar
      .replace(',', '.')
      .toLowerCase();

    // "1000 - 4999"
    let m = t.match(/(\d+)\s*[-–]\s*(\d+)/);
    if (m) return { lo: +m[1], hi: +m[2] };

    // "menos de 100"
    m = t.match(/menos\s+de\s+(\d+)/);
    if (m) return { lo: 0, hi: +m[1] };

    // número isolado
    m = t.match(/(\d+)/);
    if (m) return { lo: +m[1], hi: +m[1] };

    return { lo: 0, hi: 0 };
  }

  /* ────────────────────────────────────────────────
   * Extração de dados do JSON da API GraphQL
   * ──────────────────────────────────────────────── */

  const NAME_KEYS  = ['page_name','advertiser_name','name','entity_name','pageName'];
  const SPEND_KEYS = ['amount_spent','spend','total_spend','amount_spent_lower_bound','spendLower'];
  const ADS_KEYS   = ['ad_count','num_ads','ads_count','adCount','number_of_ads'];
  const ID_KEYS    = ['page_id','id','pageId','entity_id'];

  function isAdRecord(obj) {
    if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return false;
    const keys = Object.keys(obj);
    return NAME_KEYS.some(k => keys.includes(k)) && SPEND_KEYS.some(k => keys.includes(k));
  }

  function parseAdRecord(obj) {
    const name = NAME_KEYS.map(k => obj[k]).find(v => typeof v === 'string' && v) || '';
    if (!name) return null;

    const rawSpend = SPEND_KEYS.map(k => obj[k]).find(v => v !== undefined);
    let lo = 0, hi = 0;
    if (rawSpend !== undefined) {
      if (typeof rawSpend === 'object' && rawSpend !== null) {
        lo = +(rawSpend.lower_bound ?? rawSpend.lower ?? rawSpend.min ?? 0);
        hi = +(rawSpend.upper_bound ?? rawSpend.upper ?? rawSpend.max ?? lo);
      } else if (typeof rawSpend === 'string') {
        ({ lo, hi } = parseSpendText(rawSpend));
      } else {
        lo = hi = +rawSpend || 0;
      }
    }

    const adsCount = +(ADS_KEYS.map(k => obj[k]).find(v => v !== undefined) ?? 0);
    const pageId   = String(ID_KEYS.map(k => obj[k]).find(v => v) ?? '');

    return { name, lo, hi, adsCount, pageId, ts: Date.now() };
  }

  function extractFromJson(data, depth = 0) {
    if (depth > 20 || !data || typeof data !== 'object') return null;

    if (Array.isArray(data)) {
      // Array de registros?
      const recs = data.filter(isAdRecord).map(parseAdRecord).filter(Boolean);
      if (recs.length > 0) return recs;
      // Recursão
      for (const item of data) {
        const r = extractFromJson(item, depth + 1);
        if (r) return r;
      }
    } else {
      if (isAdRecord(data)) return [parseAdRecord(data)].filter(Boolean);
      for (const val of Object.values(data)) {
        const r = extractFromJson(val, depth + 1);
        if (r) return r;
      }
    }
    return null;
  }

  /* ────────────────────────────────────────────────
   * Interceptação de fetch
   * ──────────────────────────────────────────────── */

  const _fetch = window.fetch;
  window.fetch = async function (input, init) {
    const res = await _fetch.call(this, input, init);
    const url = (typeof input === 'string' ? input : input?.url) || '';

    if (url.includes('/api/graphql') || url.includes('/ads/library')) {
      try {
        const text = await res.clone().text();
        const json = JSON.parse(cleanFbJson(text));
        const recs = extractFromJson(json);
        if (recs) dispatch(recs);
      } catch (_) { /* não é JSON ou estrutura inesperada */ }
    }
    return res;
  };

  /* ────────────────────────────────────────────────
   * Interceptação de XMLHttpRequest
   * ──────────────────────────────────────────────── */

  const _open = XMLHttpRequest.prototype.open;
  const _send = XMLHttpRequest.prototype.send;

  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this._trackerUrl = String(url || '');
    return _open.apply(this, [method, url, ...rest]);
  };

  XMLHttpRequest.prototype.send = function (body) {
    const url = this._trackerUrl || '';
    if (url.includes('/api/graphql') || url.includes('/ads/library')) {
      this.addEventListener('readystatechange', () => {
        if (this.readyState !== 4 || this.status !== 200) return;
        try {
          const json = JSON.parse(cleanFbJson(this.responseText));
          const recs = extractFromJson(json);
          if (recs) dispatch(recs);
        } catch (_) {}
      });
    }
    return _send.call(this, body);
  };

  /* ────────────────────────────────────────────────
   * Scraping de DOM (fallback)
   * ──────────────────────────────────────────────── */

  function parseBRL(text) {
    return parseSpendText(text);
  }

  function scrapeDom() {
    const records = [];

    // Estratégia 1: linhas com role="row"
    document.querySelectorAll('[role="row"]').forEach(row => {
      const cells = [...row.querySelectorAll('[role="cell"],[role="gridcell"],td')];
      if (cells.length < 2) return;

      const texts = cells.map(c => c.textContent.trim());
      let name = '', spendText = '', ads = 0;

      texts.forEach(t => {
        if (!name && t && !t.match(/^\s*[\d$R,.\-–]+\s*$/) && t.length > 2) {
          name = t;
        } else if (!spendText && (t.includes('R$') || /menos de/i.test(t))) {
          spendText = t;
        } else if (!ads && /^\d+$/.test(t.replace(/\./g, ''))) {
          ads = +t.replace(/\./g, '');
        }
      });

      if (name && spendText) {
        const { lo, hi } = parseBRL(spendText);
        if (lo > 0 || hi > 0) {
          records.push({ name, lo, hi, adsCount: ads, pageId: '', ts: Date.now() });
        }
      }
    });

    // Estratégia 2: varre elementos com R$ e sobe na árvore para pegar o nome
    if (records.length === 0) {
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      const seen = new Set();
      let node;
      while ((node = walker.nextNode())) {
        const t = node.textContent.trim();
        if (!t.includes('R$') && !/menos de/i.test(t)) continue;
        const { lo, hi } = parseBRL(t);
        if (lo === 0 && hi === 0) continue;

        // Sobe na árvore procurando o nome do anunciante
        let el = node.parentElement;
        let candidateName = '';
        for (let d = 0; d < 6 && el; d++, el = el.parentElement) {
          const link = el.querySelector('a[href*="facebook.com"]');
          if (link) { candidateName = link.textContent.trim(); break; }
          const aria = el.querySelector('[aria-label]');
          if (aria) { candidateName = aria.getAttribute('aria-label'); break; }
        }

        if (candidateName && !seen.has(candidateName)) {
          seen.add(candidateName);
          records.push({ name: candidateName, lo, hi, adsCount: 0, pageId: '', ts: Date.now() });
        }
      }
    }

    if (records.length > 0) dispatch(records);
  }

  // Observa o DOM com debounce
  let timer;
  new MutationObserver(() => {
    clearTimeout(timer);
    timer = setTimeout(() => { try { scrapeDom(); } catch (_) {} }, 1500);
  }).observe(document.documentElement, { childList: true, subtree: true });

})();
