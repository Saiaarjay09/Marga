import { $, el, icon, fmt, store, debounce } from './util.js';
import { api } from './api.js';
import { TripMap } from './map.js';
import { batteryChart } from './chart.js';

const STYLES = [
  { value: 'Eco', label: 'Eco', hint: 'Smooth, early lift-off, gentle throttle. Uses about 7% less energy.' },
  { value: 'Normal', label: 'Normal', hint: 'Typical mixed driving at the speed the route allows.' },
  { value: 'Aggressive', label: 'Spirited', hint: 'Hard acceleration and late braking. Uses about 14% more energy.' },
];

const TUNE = [
  { key: 'battery_kwh', label: 'Usable battery', min: 10, max: 130, step: 0.5, show: (v) => `${fmt.dec(v, 1)} kWh` },
  { key: 'drag_coefficient', label: 'Drag coefficient (Cd)', min: 0.18, max: 0.45, step: 0.005, show: (v) => fmt.dec(v, 3) },
  { key: 'frontal_area_m2', label: 'Frontal area', min: 1.8, max: 3.2, step: 0.05, show: (v) => `${fmt.dec(v, 2)} m²` },
  { key: 'mass_kg', label: 'Weight with driver', min: 700, max: 3300, step: 25, show: (v) => `${fmt.int(v)} kg` },
  { key: 'mre_pct', label: 'Motor + inverter efficiency', min: 0.8, max: 0.96, step: 0.005, show: (v) => `${fmt.dec(v * 100, 1)}%` },
  { key: 'efficiency_wh_km', label: 'Flat baseline', min: 60, max: 300, step: 1, show: (v) => `${fmt.int(v)} Wh/km` },
];

const EXAMPLES = [
  { label: 'Bengaluru → Goa', a: ['Bengaluru', 12.9716, 77.5946], b: ['Panaji, Goa', 15.4909, 73.8278] },
  { label: 'Delhi → Jaipur', a: ['New Delhi', 28.6139, 77.209], b: ['Jaipur', 26.9124, 75.7873] },
  { label: 'Hyderabad → Bengaluru', a: ['Hyderabad', 17.385, 78.4867], b: ['Bengaluru', 12.9716, 77.5946] },
  { label: 'Mumbai → Goa', a: ['Mumbai', 19.076, 72.8777], b: ['Panaji, Goa', 15.4909, 73.8278] },
];

const FACTORS = [
  ['aero', 'Air resistance', 'Pushing air aside at your average speed'],
  ['wind', 'Wind', 'Headwinds cost energy, tailwinds give it back'],
  ['rolling', 'Tyres and road', 'Rolling resistance, grows with weight'],
  ['hills', 'Hills', 'Climbs, minus what regen recovers on descents'],
  ['climate', 'Climate and electronics', 'AC or heater plus accessories'],
  ['style', 'Driving style', 'Extra energy from how you accelerate'],
];

const state = {
  vehicles: [], car: null, spec: {}, places: { start: null, end: null },
  style: 'Normal', soc: 100, buffer: 15, avoid: true,
  routes: [], active: 0, plan: null, busy: false, dirty: false,
};

const map = new TripMap('map');
const panel = $('#panel');

/* ------------------------------ theme ------------------------------ */
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $('meta[name="theme-color"]').content = theme === 'dark' ? '#12141A' : '#F6F1E9';
  const btn = $('#theme-toggle');
  btn.replaceChildren(icon(theme === 'dark' ? 'sun' : 'moon'));
  map.setTheme(theme);
  if (state.plan) map.drawChargers(state.plan.chargers);
  store('marga.theme', theme);
}
applyTheme(store('marga.theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));
$('#theme-toggle').addEventListener('click', () => applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'));

/* ------------------------------ icons ------------------------------ */
$('#locate-btn').append(icon('locate'));
$('#swap-btn').append(icon('swap'));

/* ------------------------------ place search ------------------------------ */
class Combo {
  constructor(role) {
    this.role = role;
    this.input = $(`#${role}-input`);
    this.list = $(`#${role}-list`);
    this.items = [];
    this.index = -1;
    this.seq = 0;
    const search = debounce((q) => this.search(q), 260);
    this.input.addEventListener('input', () => {
      state.places[role] = null;
      markDirty();
      const q = this.input.value.trim();
      if (q.length < 2) return this.close();
      search(q);
    });
    this.input.addEventListener('keydown', (e) => this.key(e));
    this.input.addEventListener('blur', () => setTimeout(() => this.close(), 120));
    this.input.addEventListener('focus', () => { if (this.items.length && !state.places[role]) this.open(); });
  }

  async search(q) {
    const mine = ++this.seq;
    try {
      const { results } = await api.geocode(q);
      if (mine !== this.seq) return;
      this.items = results;
      this.render(results.length ? null : 'No matching place in India');
    } catch (err) {
      if (mine === this.seq) { this.items = []; this.render(err.message); }
    }
  }

  render(emptyText) {
    this.list.replaceChildren();
    this.index = -1;
    if (emptyText) this.list.append(el('li', { class: 'empty', text: emptyText }));
    this.items.forEach((item, i) => {
      const li = el('li', { role: 'option', id: `${this.role}-opt-${i}`, 'aria-selected': 'false', text: item.label });
      li.addEventListener('mousedown', (e) => { e.preventDefault(); this.pick(item); });
      this.list.append(li);
    });
    this.open();
  }

  key(e) {
    if (this.list.hidden && e.key !== 'ArrowDown') return;
    if (e.key === 'ArrowDown') { e.preventDefault(); if (this.list.hidden && this.items.length) this.open(); this.move(1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); this.move(-1); }
    else if (e.key === 'Enter' && this.index >= 0) { e.preventDefault(); this.pick(this.items[this.index]); }
    else if (e.key === 'Escape') this.close();
  }

  move(step) {
    if (!this.items.length) return;
    this.index = (this.index + step + this.items.length) % this.items.length;
    [...this.list.querySelectorAll('[role="option"]')].forEach((li, i) => li.setAttribute('aria-selected', String(i === this.index)));
    this.input.setAttribute('aria-activedescendant', `${this.role}-opt-${this.index}`);
  }

  pick(item) {
    state.places[this.role] = item;
    this.input.value = item.label;
    this.close();
    markDirty();
  }

  set(place) { state.places[this.role] = place; this.input.value = place ? place.label : ''; this.close(); }
  open() { this.list.hidden = false; this.input.setAttribute('aria-expanded', 'true'); }
  close() { this.list.hidden = true; this.input.setAttribute('aria-expanded', 'false'); }
}
const combos = { start: new Combo('start'), end: new Combo('end') };

$('#swap-btn').addEventListener('click', () => {
  const { start, end } = state.places;
  const s = combos.start.input.value, e = combos.end.input.value;
  combos.start.set(end); combos.end.set(start);
  if (!end) combos.start.input.value = e;
  if (!start) combos.end.input.value = s;
});

$('#locate-btn').addEventListener('click', () => {
  if (!navigator.geolocation) return showStatus('Your browser can\'t share a location.', true);
  showStatus('Finding you…');
  navigator.geolocation.getCurrentPosition(async (pos) => {
    try {
      const place = await api.reverse(pos.coords.latitude, pos.coords.longitude);
      combos.start.set(place);
      hideStatus();
    } catch (err) { showStatus(err.message, true); }
  }, () => showStatus('Location permission was denied. Type your starting point instead.', true), { timeout: 10000 });
});

const exBox = $('#examples');
EXAMPLES.forEach((ex) => {
  exBox.append(el('button', {
    type: 'button', class: 'chip', text: ex.label,
    onclick: () => {
      combos.start.set({ label: ex.a[0], lat: ex.a[1], lng: ex.a[2] });
      combos.end.set({ label: ex.b[0], lat: ex.b[1], lng: ex.b[2] });
      markDirty();
    },
  }));
});

/* ------------------------------ car picker ------------------------------ */
const brandOf = (name) => {
  if (name.startsWith('Custom')) return 'Custom';
  if (name.startsWith('Maruti Suzuki')) return 'Maruti Suzuki';
  if (name.startsWith('Rolls-Royce')) return 'Rolls-Royce';
  return name.split(' ')[0];
};

function renderCarList(filter = '') {
  const list = $('#car-list');
  list.replaceChildren();
  const q = filter.trim().toLowerCase();
  let lastBrand = '';
  for (const v of state.vehicles) {
    if (q && !v.name.toLowerCase().includes(q)) continue;
    const brand = brandOf(v.name);
    if (brand !== lastBrand) { list.append(el('li', { class: 'group', role: 'presentation', text: brand })); lastBrand = brand; }
    const shown = v.name.startsWith(brand) && brand !== 'Custom' ? v.name.slice(brand.length).trim() || v.name : v.name;
    const li = el('li', { class: 'opt', role: 'option', 'aria-selected': String(state.car && state.car.name === v.name) },
      el('span', { text: shown }), el('small', { text: `${fmt.dec(v.battery_kwh, 0)} kWh` }));
    li.addEventListener('click', () => { selectCar(v); closeCarPop(); });
    list.append(li);
  }
  if (!list.children.length) list.append(el('li', { class: 'group', text: 'No cars match' }));
}

function openCarPop() {
  $('#car-pop').hidden = false;
  $('#car-button').setAttribute('aria-expanded', 'true');
  $('#car-search').value = '';
  renderCarList();
  $('#car-search').focus();
}
function closeCarPop() { $('#car-pop').hidden = true; $('#car-button').setAttribute('aria-expanded', 'false'); }
$('#car-button').addEventListener('click', () => ($('#car-pop').hidden ? openCarPop() : closeCarPop()));
$('#car-search').addEventListener('input', (e) => renderCarList(e.target.value));
document.addEventListener('click', (e) => { if (!e.target.closest('.picker')) closeCarPop(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeCarPop(); });

function selectCar(v, keepSpec) {
  state.car = v;
  if (!keepSpec) {
    state.spec = Object.fromEntries([...TUNE.map((t) => t.key), 'rolling_resistance'].map((k) => [k, v[k]]));
  }
  $('#car-name').textContent = v.name;
  $('#car-meta').textContent = v.claimed_km ? `Claimed range ${v.claimed}` : 'Set your own numbers below';
  renderSpecs();
  renderTune();
  markDirty();
  store('marga.car', v.name);
  store('marga.spec', state.spec);
}

function renderSpecs() {
  const s = state.spec;
  $('#spec-grid').replaceChildren(
    ...[['Battery', `${fmt.dec(s.battery_kwh, 0)} kWh`], ['Drag Cd', fmt.dec(s.drag_coefficient, 2)], ['Weight', `${fmt.int(s.mass_kg)} kg`], ['Drivetrain', `${fmt.dec(s.mre_pct * 100, 0)}%`]]
      .map(([k, v]) => el('div', {}, el('dt', { text: k }), el('dd', { text: v }))),
  );
}

function renderTune() {
  const box = $('#tune');
  box.replaceChildren();
  TUNE.forEach((t) => {
    const id = `tune-${t.key}`;
    const out = el('output', { for: id, text: t.show(state.spec[t.key]) });
    const input = el('input', { id, type: 'range', min: t.min, max: t.max, step: t.step, value: state.spec[t.key] });
    input.addEventListener('input', () => {
      state.spec[t.key] = parseFloat(input.value);
      out.textContent = t.show(state.spec[t.key]);
      renderSpecs();
      markDirty();
      store('marga.spec', state.spec);
    });
    box.append(el('label', { class: 'slider-row', for: id }, el('span', { text: t.label }), out, input));
  });
}

$('#tune-toggle').addEventListener('click', (e) => {
  const open = $('#tune').hidden;
  $('#tune').hidden = !open;
  e.currentTarget.setAttribute('aria-expanded', String(open));
});

/* ------------------------------ driving controls ------------------------------ */
function renderStyles() {
  const seg = $('#style-seg');
  seg.replaceChildren();
  STYLES.forEach((s) => {
    const b = el('button', { type: 'button', role: 'radio', 'aria-checked': String(state.style === s.value), text: s.label });
    b.addEventListener('click', () => { state.style = s.value; renderStyles(); markDirty(); store('marga.style', s.value); });
    seg.append(b);
  });
  $('#style-hint').textContent = STYLES.find((s) => s.value === state.style).hint;
}

const bindSlider = (id, outId, key, suffix) => {
  const input = $(id), out = $(outId);
  input.addEventListener('input', () => {
    state[key] = parseFloat(input.value);
    out.textContent = `${state[key]}${suffix}`;
    markDirty();
    store(`marga.${key}`, state[key]);
  });
};
bindSlider('#soc-input', '#soc-out', 'soc', '%');
bindSlider('#buffer-input', '#buffer-out', 'buffer', '%');
$('#avoid-input').addEventListener('change', (e) => { state.avoid = e.target.checked; markDirty(); store('marga.avoid', state.avoid); });

/* ------------------------------ status + sheet ------------------------------ */
function showStatus(text, isError = false) {
  const box = $('#status');
  box.hidden = false;
  box.className = `status${isError ? ' error' : ''}`;
  box.replaceChildren(...(isError ? [] : [el('span', { class: 'spinner' })]), el('span', { text }));
}
function hideStatus() { $('#status').hidden = true; }

const isMobile = () => window.innerWidth <= 860;
function setPeek(peek) { panel.classList.toggle('is-peek', peek); setTimeout(() => map.invalidate(), 320); }
$('#sheet-handle').addEventListener('click', () => setPeek(!panel.classList.contains('is-peek')));
$('#peek-summary').addEventListener('click', () => setPeek(false));

function markDirty() {
  if (!state.plan || state.busy) return;
  state.dirty = true;
  const bar = $('#dirty');
  if (bar) bar.hidden = false;
}

/* ------------------------------ planning ------------------------------ */
async function resolvePlace(role) {
  if (state.places[role]) return state.places[role];
  const q = combos[role].input.value.trim();
  if (q.length < 2) throw new Error(role === 'start' ? 'Where are you starting from?' : 'Where are you heading?');
  const { results } = await api.geocode(q);
  if (!results.length) throw new Error(`Couldn't find "${q}" in India. Try a nearby city.`);
  combos[role].set(results[0]);
  return results[0];
}

function setBusy(busy, label) {
  state.busy = busy;
  $('#plan-btn').disabled = busy;
  $('#plan-label').textContent = busy ? label : state.plan ? 'Plan again' : 'Plan my trip';
}

function vehiclePayload() {
  return { name: state.car.name, ...state.spec };
}

async function planTrip() {
  if (state.busy) return;
  try {
    setBusy(true, 'Finding routes…');
    showStatus('Finding the best roads between your places…');
    const [start, end] = [await resolvePlace('start'), await resolvePlace('end')];
    state.endpoints = { start, end };
    const { routes } = await api.routes({ lat: start.lat, lng: start.lng }, { lat: end.lat, lng: end.lng });
    state.routes = routes;
    state.active = 0;
    await planRoute(0);
  } catch (err) {
    showStatus(err.message, true);
    setBusy(false);
  }
}

async function planRoute(index) {
  state.active = index;
  setBusy(true, 'Reading terrain and weather…');
  showStatus('Reading terrain and weather along the route, then simulating every kilometre…');
  const route = state.routes[index];
  const payload = {
    route_id: route.id, vehicle: vehiclePayload(), driver_style: state.style,
    safety_buffer_pct: state.buffer, start_soc_pct: state.soc, avoid_down: state.avoid,
  };
  try {
    state.plan = await api.plan(payload);
    state.dirty = false;
    hideStatus();
    renderResults();
    drawOnMap();
  } catch (err) {
    showStatus(err.message, true);
    map.showRoutes(state.routes, state.active, planRoute);
  } finally {
    setBusy(false);
  }
}

function drawOnMap() {
  map.clearOverlays();
  map.showRoutes(state.routes, state.active, planRoute);
  map.drawEndpoints(state.endpoints.start, state.endpoints.end);
  map.drawChargers(state.plan.chargers);
  map.drawStops(state.plan.stops, (i) => document.getElementById(`stop-${i}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' }));
}

/* ------------------------------ results ------------------------------ */
function tile(label, value, unit, sub) {
  return el('div', { class: 'tile' }, el('small', { text: label }), el('b', {}, value, unit ? el('em', { text: ` ${unit}` }) : null), sub ? el('small', { text: sub }) : null);
}

function renderResults() {
  const p = state.plan;
  const box = $('#results');
  box.hidden = false;
  box.replaceChildren();
  $('#intro').classList.add('compact');

  const dirty = el('div', { class: 'dirty', id: 'dirty', hidden: true },
    el('span', { text: 'Settings changed' }),
    el('button', { type: 'button', text: 'Update plan', onclick: () => planRoute(state.active) }));
  box.append(dirty);

  if (state.routes.length > 1) {
    const chips = el('div', { class: 'route-chips', role: 'group', 'aria-label': 'Route alternatives' });
    state.routes.forEach((r, i) => chips.append(el('button', {
      type: 'button', class: 'route-chip', 'aria-pressed': String(i === state.active), onclick: () => i !== state.active && planRoute(i),
    }, el('strong', { text: `${r.label}${i === 0 ? ' · fastest' : ''}` }), `${fmt.km(r.distance_km)} · ${fmt.mins(r.duration_mins)}`)));
    box.append(chips);
  }
  if (state.routes[state.active].fallback) {
    box.append(el('div', { class: 'status error' }, el('span', { text: 'The road-routing service is unreachable, so this is a straight-line estimate. Real roads will be longer.' })));
  }

  if (p.status !== 'success') {
    box.append(el('div', { class: 'status error' }, el('span', { text: p.message })));
  }

  const s = p.summary;
  const reference = s.claimed_range_km || s.baseline_range_km;
  const delta = ((s.physics_range_km - reference) / reference) * 100;
  const maxRange = Math.max(s.physics_range_km, s.claimed_range_km || 0, s.baseline_range_km);
  const barRow = (label, val, cls) => el('div', { class: `bar-row ${cls || ''}` },
    el('span', { text: label }), el('div', { class: 'track' }, el('div', { class: 'fill', style: `width:${(val / maxRange) * 100}%` })), el('span', { class: 'val', text: fmt.km(val) }));

  const hero = el('section', { class: 'res-card hero' },
    el('h3', { class: 'res-title', text: 'Your real range on this trip' }),
    el('div', {}, el('span', { class: 'big', text: fmt.int(s.physics_range_km) }), el('span', { class: 'unit', text: 'km' }),
      el('span', { class: `delta${delta < 0 ? ' neg' : ''}`, text: `${delta >= 0 ? '+' : ''}${Math.round(delta)}% vs ${s.claimed_range_km ? 'claimed' : 'flat estimate'}` })),
    el('p', { class: 'lead', text: 'From a full battery, over this exact road, weather and elevation.' }),
    el('div', { class: 'bars' },
      s.claimed_range_km ? barRow('Claimed', s.claimed_range_km) : null,
      barRow('Flat estimate', s.baseline_range_km),
      barRow('Marga', s.physics_range_km, 'marga')));
  box.append(hero);

  if (p.status === 'success') {
    box.append(el('section', { class: 'res-card' },
      el('div', { class: 'tiles' },
        tile('Distance', fmt.int(s.distance_km), 'km'),
        tile('Total time', fmt.mins(s.total_mins), '', `${fmt.mins(s.drive_mins)} driving${s.charge_mins ? ` + ${fmt.mins(s.charge_mins)} charging` : ''}`),
        tile('Charging stops', String(s.stops), '', s.stops ? '' : 'Straight through'),
        tile('Arrive with', s.arrival_soc_pct == null ? '–' : fmt.int(Math.max(s.arrival_soc_pct, 0)), '%', `${fmt.dec(s.avg_wh_km, 0)} Wh/km average`))));
  }

  box.append(itinerary(p, s));

  if (p.profile) {
    box.append(el('section', { class: 'res-card' },
      el('h3', { class: 'res-title', text: 'Battery and terrain' }),
      batteryChart(p.profile, p.stops, state.buffer, (km) => map.showCursorAtKm(km), () => map.hideCursor())));
  }

  box.append(factorsCard(p));
  box.append(conditionsCard(p));

  const mapsUrl = googleMapsUrl();
  box.append(el('div', { class: 'actions' },
    el('a', { class: 'btn', href: mapsUrl, target: '_blank', rel: 'noopener noreferrer' }, icon('maps'), 'Open in Google Maps'),
    el('button', { type: 'button', class: 'btn ghost', onclick: () => map.fitAll(), text: 'Show whole route' })));

  const r = p.registry;
  $('#foot').replaceChildren(
    el('div', { text: `Charger data: Open Charge Map, refreshed ${fmt.date(r.last_updated)}. ${fmt.int(r.health_checked)} stations health-checked, ${fmt.int(r.flagged_down)} flagged down.` }),
    el('div', { text: 'Estimates come from a physics model, not a guarantee. Keep a margin and check a station is free before you commit.' }));

  if (isMobile()) {
    const ps = $('#peek-summary');
    ps.hidden = false;
    ps.replaceChildren(el('b', { text: `${fmt.int(s.physics_range_km)} km range` }), el('span', { text: `${s.stops} stop${s.stops === 1 ? '' : 's'} · ${fmt.mins(s.total_mins)}` }));
    setPeek(true);
  }
  requestAnimationFrame(() => box.scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' }));
}

function healthChip(c) {
  if (!c.checked) return el('span', { class: 'health unchecked', text: 'Not checked yet' });
  return c.is_working ? el('span', { class: 'health', text: 'Reported working' }) : el('span', { class: 'health down', text: 'Flagged down' });
}

function itinerary(p, s) {
  const list = el('ol', { class: 'timeline' });
  const { start, end } = state.endpoints;
  list.append(el('li', { class: 'tl-item' }, el('span', { class: 'tl-node start' }),
    el('div', { class: 'tl-head', text: start.label }), el('div', { class: 'tl-sub', text: `Leave with ${state.soc}%` })));

  p.stops.forEach((st) => {
    const meter = el('div', { class: 'meter' },
      el('i', { style: `width:${Math.min(100, st.depart_soc_pct)}%` }), el('i', { class: 'now', style: `width:${Math.max(0, Math.min(100, st.arrival_soc_pct))}%` }));
    const card = el('button', { type: 'button', class: 'stop-card', id: `stop-${st.index}`, onclick: () => map.focusStop(st.index) },
      el('div', { class: 'name', text: st.name }),
      el('div', { class: 'stop-meta' },
        el('span', {}, el('b', { text: fmt.km(st.at_km) }), ' in'),
        el('span', {}, el('b', { text: `${fmt.int(st.power_kw)} kW` })),
        el('span', {}, '~', el('b', { text: fmt.mins(st.charge_minutes) }), ' charging'),
        st.detour_km > 0.5 ? el('span', {}, el('b', { text: `${fmt.dec(st.detour_km, 1)} km` }), ' off route') : null,
        healthChip(st)),
      el('div', { class: 'soc-line' }, el('span', { text: `${fmt.pct(Math.max(0, st.arrival_soc_pct))}` }), meter, el('span', { text: fmt.pct(st.depart_soc_pct) })),
      st.note ? el('div', { class: 'note', text: st.note }) : null,
      st.backup ? el('div', { class: 'backup-row' }, el('i', { class: 'diamond' }),
        el('div', {}, el('b', { text: `Backup: ${st.backup.name}` }), el('div', { text: `${fmt.int(st.backup.power_kw)} kW, ${fmt.dec(st.backup_gap_km, 0)} km from this stop` }))) : null);
    list.append(el('li', { class: 'tl-item' }, el('span', { class: 'tl-node stop', text: String(st.index) }),
      el('div', { class: 'tl-sub', text: `Stop ${st.index} · ${fmt.mins(st.eta_min)} into the trip` }), card));
  });

  list.append(el('li', { class: 'tl-item' }, el('span', { class: 'tl-node end' }),
    el('div', { class: 'tl-head', text: end.label }),
    el('div', { class: 'tl-sub', text: p.status === 'success' ? `Arrive after ${fmt.mins(s.total_mins)} with ${fmt.pct(Math.max(0, s.arrival_soc_pct))}` : 'Trip could not be completed' })));

  return el('section', { class: 'res-card' }, el('h3', { class: 'res-title', text: 'Your itinerary' }), list);
}

function factorsCard(p) {
  const f = p.factors_kwh;
  const maxAbs = Math.max(...FACTORS.map(([k]) => Math.abs(f[k] || 0)), 0.1);
  const rows = FACTORS.map(([key, name, sub]) => {
    const v = f[key] || 0;
    const width = (Math.abs(v) / maxAbs) * 100;
    const shown = Math.abs(v) < 0.05 ? '~0 kWh' : v < 0 ? `${fmt.dec(-v, 1)} kWh back` : `${fmt.dec(v, 1)} kWh`;
    return el('div', { class: 'factor' },
      el('div', { class: 'lbl' }, name, el('small', { text: sub })), el('div', { class: 'amt', text: shown }),
      el('div', { class: 'track' }, el('div', { class: `fill${v < 0 ? ' credit' : ''}`, style: `left:0;width:${width}%` })));
  });
  return el('section', { class: 'res-card' },
    el('h3', { class: 'res-title', text: `Where ${fmt.dec(p.summary.trip_energy_kwh, 0)} kWh goes` }), ...rows);
}

function conditionsCard(p) {
  const w = p.weather;
  const temp = Math.round(w.temp_min) === Math.round(w.temp_max) ? `${Math.round(w.temp_min)}°C` : `${Math.round(w.temp_min)}–${Math.round(w.temp_max)}°C`;
  const net = w.elevation_change_m;
  return el('section', { class: 'res-card' },
    el('h3', { class: 'res-title', text: 'Conditions on the way' }),
    el('div', { class: 'tiles' },
      tile('Weather', temp, '', w.description),
      tile('Strongest wind', fmt.int(w.wind_kmh), 'km/h'),
      tile('Net elevation', `${net >= 0 ? '+' : '−'}${fmt.int(Math.abs(net))}`, 'm', `${fmt.int(w.elevation_min_m)}–${fmt.int(w.elevation_max_m)} m range`),
      tile('Average speed', fmt.int(p.summary.avg_speed_kmh), 'km/h')),
    el('p', { class: 'explain', text: p.explanation }));
}

function googleMapsUrl() {
  const { start, end } = state.endpoints;
  const params = new URLSearchParams({
    api: '1', origin: `${start.lat},${start.lng}`, destination: `${end.lat},${end.lng}`, travelmode: 'driving',
  });
  const way = state.plan.stops.slice(0, 9).map((s) => `${s.lat},${s.lng}`).join('|');
  if (way) params.set('waypoints', way);
  return `https://www.google.com/maps/dir/?${params}`;
}

/* ------------------------------ boot ------------------------------ */
$('#trip-form').addEventListener('submit', (e) => { e.preventDefault(); planTrip(); });
window.addEventListener('resize', debounce(() => map.invalidate(), 150));

async function boot() {
  state.style = store('marga.style') || 'Normal';
  state.soc = store('marga.soc') ?? 100;
  state.buffer = store('marga.buffer') ?? 15;
  state.avoid = store('marga.avoid') ?? true;
  $('#soc-input').value = state.soc; $('#soc-out').textContent = `${state.soc}%`;
  $('#buffer-input').value = state.buffer; $('#buffer-out').textContent = `${state.buffer}%`;
  $('#avoid-input').checked = state.avoid;
  renderStyles();

  try {
    const { vehicles } = await api.vehicles();
    state.vehicles = vehicles;
    const savedName = store('marga.car');
    const car = vehicles.find((v) => v.name === savedName) || vehicles.find((v) => v.name === 'Tata Nexon EV (30 kWh)') || vehicles[0];
    const savedSpec = store('marga.spec');
    selectCar(car);
    if (savedSpec && car.name === savedName) { state.spec = { ...state.spec, ...savedSpec }; renderSpecs(); renderTune(); }
    state.dirty = false;
  } catch (err) {
    $('#car-name').textContent = 'Could not load cars';
    showStatus(err.message, true);
  }

  try {
    const st = await api.status();
    $('#foot').replaceChildren(el('div', { text: `${fmt.int(st.chargers)} fast chargers across India, refreshed ${fmt.date(st.registry_last_updated)}.` }));
  } catch { /* footer is optional */ }
}
boot();
