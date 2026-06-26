/**
 * background.js  —  Service Worker (MV3)
 * Armazena os dados capturados pelos content scripts.
 */

const KEY = 'rn_ad_tracker_v2';

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === 'AD_DATA') save(msg.records);
});

async function save(records) {
  if (!records?.length) return;

  const { [KEY]: store = { candidates: {}, lastUpdated: null } } =
    await chrome.storage.local.get(KEY);

  const batchTs = Date.now();
  let changed = false;

  for (const rec of records) {
    const key = rec.pageId || rec.name;
    if (!key) continue;

    if (!store.candidates[key]) {
      store.candidates[key] = { name: rec.name, pageId: rec.pageId, history: [] };
    }

    const cand = store.candidates[key];
    cand.name = rec.name;

    // Só acrescenta se diferente do último snapshot
    const last = cand.history.at(-1);
    if (!last || last.lo !== rec.lo || last.hi !== rec.hi || last.ads !== rec.adsCount) {
      cand.history.push({ lo: rec.lo, hi: rec.hi, ads: rec.adsCount, ts: rec.ts || batchTs });
      if (cand.history.length > 120) cand.history = cand.history.slice(-120);
      changed = true;
    }

    cand.lo  = rec.lo;
    cand.hi  = rec.hi;
    cand.ads = rec.adsCount;
    cand.updatedAt = batchTs;
  }

  if (changed) {
    store.lastUpdated = batchTs;
    await chrome.storage.local.set({ [KEY]: store });
    chrome.runtime.sendMessage({ type: 'REFRESHED' }).catch(() => {});
  }
}
