import { el, fmt } from './util.js';

const NS = 'http://www.w3.org/2000/svg';
const svg = (tag, attrs = {}) => {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
};

/**
 * Battery-and-terrain chart. Blue line = state of charge along the route,
 * grey area = elevation, dashed red = your arrival buffer, numbered ticks = stops.
 * Hovering (or touching) moves a dot along the map's route.
 */
export function batteryChart(profile, stops, bufferPct, onHover, onLeave) {
  const W = 400, H = 190, m = { l: 34, r: 10, t: 22, b: 24 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const { km, elev, soc } = profile;
  const maxKm = km[km.length - 1] || 1;
  const eMin = Math.min(...elev), eMax = Math.max(...elev);
  const ePad = Math.max((eMax - eMin) * 0.15, 20);
  const x = (v) => m.l + (v / maxKm) * iw;
  const ySoc = (v) => m.t + ih - (Math.max(0, Math.min(100, v)) / 100) * ih;
  const yEl = (v) => m.t + ih - ((v - (eMin - ePad)) / ((eMax + ePad) - (eMin - ePad))) * ih * 0.55;

  const root = svg('svg', { viewBox: `0 0 ${W} ${H}`, class: 'chart', role: 'img', 'aria-label': 'Battery level and elevation along the route' });

  for (const g of [0, 25, 50, 75, 100]) {
    root.append(svg('line', { class: 'grid', x1: m.l, x2: W - m.r, y1: ySoc(g), y2: ySoc(g) }));
    const t = svg('text', { x: m.l - 6, y: ySoc(g) + 4, 'text-anchor': 'end' });
    t.textContent = `${g}%`;
    root.append(t);
  }
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const v = (maxKm * i) / ticks;
    const t = svg('text', { x: x(v), y: H - 6, 'text-anchor': i === 0 ? 'start' : i === ticks ? 'end' : 'middle' });
    t.textContent = `${Math.round(v)} km`;
    root.append(t);
  }

  const pts = elev.map((e, i) => `${x(km[i]).toFixed(1)} ${yEl(e).toFixed(1)}`);
  const area = `M ${x(km[0]).toFixed(1)} ${m.t + ih} L ${pts.join(' L ')} L ${x(km[km.length - 1]).toFixed(1)} ${m.t + ih} Z`;
  root.append(svg('path', { class: 'elev', d: area, opacity: 0.85 }));
  root.append(svg('path', { class: 'elev-line', d: `M ${pts.join(' L ')}` }));

  root.append(svg('line', { class: 'buffer', x1: m.l, x2: W - m.r, y1: ySoc(bufferPct), y2: ySoc(bufferPct) }));

  let line = '';
  soc.forEach((s, i) => { line += `${i ? ' L' : 'M'} ${x(km[i]).toFixed(1)} ${ySoc(s).toFixed(1)}`; });
  root.append(svg('path', { class: 'soc', d: line }));

  for (const s of stops) {
    const sx = x(s.at_km);
    root.append(svg('line', { class: 'stop-tick', x1: sx, x2: sx, y1: m.t - 4, y2: m.t + ih }));
    root.append(svg('circle', { class: 'stop-dot', cx: sx, cy: m.t - 8, r: 8 }));
    const n = svg('text', { class: 'stop-num', x: sx, y: m.t - 4.5 });
    n.textContent = s.index;
    root.append(n);
  }

  const cursor = svg('line', { class: 'cursor', y1: m.t, y2: m.t + ih, x1: 0, x2: 0, visibility: 'hidden' });
  root.append(cursor);

  const tip = el('div', { class: 'chart-tip', hidden: true });
  const wrap = el('div', { class: 'chart-wrap' }, root, tip);

  const move = (evt) => {
    const rect = root.getBoundingClientRect();
    const px = ((evt.touches ? evt.touches[0].clientX : evt.clientX) - rect.left) / rect.width * W;
    const kmAt = Math.max(0, Math.min(maxKm, ((px - m.l) / iw) * maxKm));
    let lo = 0, hi = km.length - 1;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (km[mid] < kmAt) lo = mid + 1; else hi = mid; }
    cursor.setAttribute('x1', x(km[lo])); cursor.setAttribute('x2', x(km[lo])); cursor.setAttribute('visibility', 'visible');
    tip.hidden = false;
    tip.textContent = `${Math.round(km[lo])} km · ${Math.round(Math.max(soc[lo], 0))}% · ${Math.round(elev[lo])} m`;
    tip.style.left = `${Math.max(14, Math.min(88, (x(km[lo]) / W) * 100))}%`;
    onHover(km[lo]);
  };
  const leave = () => { cursor.setAttribute('visibility', 'hidden'); tip.hidden = true; onLeave(); };
  root.addEventListener('pointermove', move);
  root.addEventListener('pointerleave', leave);
  root.addEventListener('touchmove', move, { passive: true });
  root.addEventListener('touchend', leave);

  const key = el('div', { class: 'chart-key' },
    el('span', {}, el('i', { style: 'background:var(--route)' }), 'Battery'),
    el('span', {}, el('i', { style: 'background:var(--surface-3)' }), `Terrain (${fmt.int(eMin)}–${fmt.int(eMax)} m)`),
    el('span', {}, el('i', { style: 'background:var(--stop)' }), 'Your buffer'),
  );
  return el('div', {}, wrap, key);
}
