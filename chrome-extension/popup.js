/** popup.js — Dashboard do RN Ad Tracker */

const STORAGE_KEY = 'rn_ad_tracker_v2';

/* ── Formatação ──────────────────────────────────────── */
const BRL  = v => (+v || 0).toLocaleString('pt-BR', { style:'currency', currency:'BRL', maximumFractionDigits:0 });
const NUM  = v => (+v || 0).toLocaleString('pt-BR');
const DATE = ts => ts ? new Date(ts).toLocaleString('pt-BR', { day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit' }) : '—';
const DT   = ts => ts ? new Date(ts).toLocaleDateString('pt-BR', { day:'2-digit',month:'2-digit' }) : '';

/* ── Estado global ───────────────────────────────────── */
let candidates = [];   // candidatos ordenados por gasto

/* ── Carrega e renderiza ─────────────────────────────── */
async function load() {
  const res = await chrome.storage.local.get(STORAGE_KEY);
  const store = res[STORAGE_KEY];

  if (!store || !Object.keys(store.candidates || {}).length) {
    show('empty'); return;
  }

  candidates = Object.values(store.candidates)
    .filter(c => c.lo !== undefined)
    .sort((a, b) => (b.lo || 0) - (a.lo || 0));

  if (!candidates.length) { show('empty'); return; }

  show('dashboard');

  /* Stats */
  const totalSpend = candidates.reduce((s, c) => s + (c.lo || 0), 0);
  const totalAds   = candidates.reduce((s, c) => s + (c.ads || 0), 0);
  document.getElementById('s-cands').textContent = candidates.length;
  document.getElementById('s-spend').textContent = BRL(totalSpend);
  document.getElementById('s-ads').textContent   = NUM(totalAds);
  document.getElementById('updated-label').textContent = 'Atualizado: ' + DATE(store.lastUpdated);

  /* Gráficos */
  requestAnimationFrame(() => {
    drawBar(document.getElementById('bar-chart'), candidates.slice(0, 12));
    populateSel(candidates);
    drawLine(document.getElementById('line-chart'), candidates[0]);
  });

  /* Tabela */
  renderTable(candidates);
}

function show(id) {
  document.getElementById('empty').classList.toggle('hidden', id !== 'empty');
  document.getElementById('dashboard').classList.toggle('hidden', id !== 'dashboard');
}

/* ── Gráfico de barras (Canvas) ──────────────────────── */
function drawBar(canvas, list) {
  if (!list.length) return;
  const DPR = devicePixelRatio || 1;
  const LABEL = 180, VAL = 110, GAP = 5, BAR_H = 22;
  const W  = canvas.parentElement.clientWidth - 28;
  const CH = W - LABEL - VAL - 8;
  const H  = (BAR_H + GAP) * list.length + 10;

  canvas.style.width  = W + 'px';
  canvas.style.height = H + 'px';
  canvas.width  = W * DPR;
  canvas.height = H * DPR;

  const ctx = canvas.getContext('2d');
  ctx.scale(DPR, DPR);

  const max = list[0]?.lo || 1;
  const COLORS = ['#3b82f6','#06b6d4','#8b5cf6','#ec4899','#f59e0b',
    '#22c55e','#ef4444','#a855f7','#14b8a6','#f97316','#6366f1','#84cc16'];

  list.forEach((c, i) => {
    const y   = i * (BAR_H + GAP) + 5;
    const bw  = ((c.lo || 0) / max) * CH;
    const col = COLORS[i % COLORS.length];

    /* track */
    ctx.fillStyle = 'rgba(51,65,85,.5)';
    ctx.beginPath(); ctx.roundRect(LABEL, y + 5, CH, BAR_H - 10, 3); ctx.fill();

    /* bar */
    if (bw > 0) {
      ctx.fillStyle = col;
      ctx.beginPath(); ctx.roundRect(LABEL, y + 5, bw, BAR_H - 10, 3); ctx.fill();
    }

    /* nome */
    ctx.fillStyle = '#f1f5f9';
    ctx.font = '500 11px Segoe UI,system-ui,sans-serif';
    ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
    const nm = c.name.length > 26 ? c.name.slice(0, 26) + '…' : c.name;
    ctx.fillText(nm, LABEL - 6, y + BAR_H / 2);

    /* valor */
    ctx.fillStyle = col;
    ctx.font = '600 11px Segoe UI,system-ui,sans-serif';
    ctx.textAlign = 'left';
    ctx.fillText(BRL(c.lo), LABEL + CH + 6, y + BAR_H / 2);
  });
}

/* ── Gráfico de linha (Canvas) ───────────────────────── */
function drawLine(canvas, cand) {
  const hist = (cand?.history || []).slice(-30);
  const DPR = devicePixelRatio || 1;
  const W = canvas.parentElement.clientWidth - 28;
  const H = 130;
  canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
  canvas.width = W * DPR; canvas.height = H * DPR;
  const ctx = canvas.getContext('2d');
  ctx.scale(DPR, DPR);

  if (hist.length < 2) {
    ctx.fillStyle = '#64748b'; ctx.font = '12px system-ui';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText('Sem dados históricos (visite mais vezes para acumular)', W / 2, H / 2);
    return;
  }

  const PAD = { t:10, r:10, b:28, l:80 };
  const cW  = W - PAD.l - PAD.r;
  const cH  = H - PAD.t - PAD.b;
  const vals = hist.map(h => h.lo || 0);
  const maxV = Math.max(...vals) || 1;
  const minV = Math.min(...vals);
  const xS   = cW / (hist.length - 1);

  /* grade */
  [0, 0.25, 0.5, 0.75, 1].forEach(f => {
    const y = PAD.t + cH - f * cH;
    ctx.strokeStyle = 'rgba(51,65,85,.5)'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(PAD.l, y); ctx.lineTo(PAD.l + cW, y); ctx.stroke();
    ctx.fillStyle = '#64748b'; ctx.font = '9px system-ui';
    ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
    ctx.fillText(BRL(minV + (maxV - minV) * f), PAD.l - 4, y);
  });

  const px = (i) => PAD.l + i * xS;
  const py = (v) => PAD.t + cH - ((v - minV) / ((maxV - minV) || 1)) * cH;

  /* área */
  ctx.beginPath();
  hist.forEach((h, i) => i === 0 ? ctx.moveTo(px(i), py(h.lo)) : ctx.lineTo(px(i), py(h.lo)));
  ctx.lineTo(px(hist.length - 1), PAD.t + cH);
  ctx.lineTo(px(0), PAD.t + cH);
  ctx.closePath();
  const grad = ctx.createLinearGradient(0, PAD.t, 0, PAD.t + cH);
  grad.addColorStop(0, 'rgba(59,130,246,.35)'); grad.addColorStop(1, 'rgba(59,130,246,0)');
  ctx.fillStyle = grad; ctx.fill();

  /* linha */
  ctx.beginPath(); ctx.strokeStyle = '#3b82f6'; ctx.lineWidth = 2.5;
  ctx.lineJoin = 'round'; ctx.lineCap = 'round';
  hist.forEach((h, i) => i === 0 ? ctx.moveTo(px(i), py(h.lo)) : ctx.lineTo(px(i), py(h.lo)));
  ctx.stroke();

  /* pontos */
  hist.forEach((h, i) => {
    ctx.beginPath(); ctx.arc(px(i), py(h.lo), 3, 0, Math.PI * 2);
    ctx.fillStyle = '#3b82f6'; ctx.fill();
  });

  /* labels eixo X */
  const idxs = [0, Math.floor(hist.length / 2), hist.length - 1];
  ctx.fillStyle = '#64748b'; ctx.font = '9px system-ui';
  ctx.textAlign = 'center'; ctx.textBaseline = 'top';
  idxs.forEach(i => { if (hist[i]) ctx.fillText(DT(hist[i].ts), px(i), PAD.t + cH + 5); });
}

/* ── Select de candidatos ────────────────────────────── */
function populateSel(list) {
  const sel = document.getElementById('cand-sel');
  sel.innerHTML = '';
  list.slice(0, 20).forEach(c => {
    const o = document.createElement('option');
    o.value = c.pageId || c.name;
    o.textContent = c.name.length > 28 ? c.name.slice(0, 28) + '…' : c.name;
    sel.appendChild(o);
  });
}

function updateLine() {
  const key  = document.getElementById('cand-sel').value;
  const cand = candidates.find(c => (c.pageId || c.name) === key) || candidates[0];
  requestAnimationFrame(() => drawLine(document.getElementById('line-chart'), cand));
}

/* ── Tabela ──────────────────────────────────────────── */
function renderTable(list) {
  const tbody = document.getElementById('tbl-body');
  tbody.innerHTML = '';
  const max = list[0]?.lo || 1;

  list.forEach((c, i) => {
    const pct = Math.round(((c.lo || 0) / max) * 100);
    const tr  = document.createElement('tr');
    tr.innerHTML = `
      <td class="c-rank">${i + 1}</td>
      <td class="c-name" title="${c.name}">${c.name}</td>
      <td class="c-spend">
        <div class="spend-wrap">
          ${BRL(c.lo)}${c.hi > c.lo ? ' – ' + BRL(c.hi) : ''}
          <div class="spend-bar" style="width:${pct}%"></div>
        </div>
      </td>
      <td class="c-ads">${NUM(c.ads)}</td>
    `;
    tbody.appendChild(tr);
  });
}

/* ── Exportar CSV ────────────────────────────────────── */
function exportCSV() {
  if (!candidates.length) return;
  const rows = [['#','Candidato','Gasto Mínimo (R$)','Gasto Máximo (R$)','Anúncios']];
  candidates.forEach((c, i) =>
    rows.push([i + 1, `"${c.name.replace(/"/g,'""')}"`, c.lo || 0, c.hi || 0, c.ads || 0]));

  const csv  = rows.map(r => r.join(',')).join('\n');
  const blob = new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8' });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href = url;
  a.download = `rn-ads-${new Date().toISOString().slice(0,10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

/* ── Eventos ─────────────────────────────────────────── */
document.getElementById('clear-btn').addEventListener('click', async () => {
  if (!confirm('Limpar todos os dados coletados?')) return;
  await chrome.storage.local.remove(STORAGE_KEY);
  show('empty');
  candidates = [];
});

document.getElementById('export-btn').addEventListener('click', exportCSV);
document.getElementById('cand-sel').addEventListener('change', updateLine);

/* Atualiza se o background coletar dados enquanto o popup está aberto */
chrome.runtime.onMessage.addListener(msg => { if (msg.type === 'REFRESHED') load(); });

/* Init */
load();
