// Client-side implementation of the same contract server.py's /api/* routes
// used to provide. Nothing here changed in app.js, map.js or chart.js: this
// module just does the work (routing, elevation, weather, physics,
// charger-corridor matching, trip simulation) in the browser instead of
// over the network, calling the same free, CORS-enabled public services
// server.py called from Python. See docs/HOW_IT_WORKS.md for why.

import { VEHICLE_DB } from './vehicles.js';
import { DRIVER_STYLE_MULTIPLIERS, flatBaselineRangeKm } from './physics.js';
import { getOsrmRoute } from './routing.js';
import { getElevationProfile } from './elevation.js';
import { sampleWeatherAlongRoute } from './weather.js';
import { loadRegistry } from './chargers.js';
import { simulateTrip } from './simulator.js';
import { nearestRoutePoint } from './geo.js';
import { explainRangeAdjustment } from './explain.js';

const INDIA_BOUNDS = { latMin: 6.0, latMax: 37.5, lngMin: 68.0, lngMax: 98.5 };
const GEOCODE_URL = 'https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer';
const CORRIDOR_KM = 10.0;

function inIndia(lat, lng) {
  const b = INDIA_BOUNDS;
  return lat >= b.latMin && lat <= b.latMax && lng >= b.lngMin && lng <= b.lngMax;
}

function claimedKm(text) {
  const m = /(\d+)\s*km/.exec(text || '');
  return m ? parseInt(m[1], 10) : null;
}

function round5(n) { return Math.round(n * 1e5) / 1e5; }
function round2(n) { return Math.round(n * 100) / 100; }
function round1(n) { return Math.round(n * 10) / 10; }

async function fetchJsonWithTimeout(url, timeoutMs) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, { signal: ctrl.signal });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

function chargerBrief(c) {
  if (!c) return null;
  const a = c.AddressInfo || {};
  return {
    name: a.Title || 'Charging station', address: a.AddressLine1 || '',
    operator: (c.OperatorInfo || {}).Title || '',
    lat: a.Latitude, lng: a.Longitude,
    power_kw: c.max_ccs2_power || 0, is_working: c.is_working !== false,
    checked: Boolean(c.health_last_checked), health_reason: c.health_reason || '',
  };
}

// In-memory route store: the browser already holds the full geometry it
// just fetched, so (unlike server.py, which had to hold it for the next
// request) this only needs to survive until the matching plan() call.
const routeStore = new Map();
let routeSeq = 0;

function corridorChargers(entry, registry) {
  if (entry.corridor_version === registry.version) return entry.corridor;
  const geometry = entry.geometry;
  const lats = geometry.map((p) => p[1]), lngs = geometry.map((p) => p[0]);
  const pad = CORRIDOR_KM / 100.0;
  const latLo = Math.min(...lats) - pad, latHi = Math.max(...lats) + pad;
  const lngLo = Math.min(...lngs) - pad, lngHi = Math.max(...lngs) + pad;
  const step = Math.max(1, Math.floor(geometry.length / 1500));
  const dlat = [], dlng = [];
  for (let i = 0; i < geometry.length; i += step) { dlat.push(geometry[i][1]); dlng.push(geometry[i][0]); }

  const corridor = [];
  for (const c of registry.chargers) {
    const a = c.AddressInfo || {};
    const clat = a.Latitude, clng = a.Longitude;
    if (clat == null || clng == null) continue;
    if (!(clat >= latLo && clat <= latHi && clng >= lngLo && clng <= lngHi)) continue;
    const [, dist] = nearestRoutePoint(dlat, dlng, clat, clng);
    if (dist <= CORRIDOR_KM) corridor.push(c);
  }
  entry.corridor = corridor;
  entry.corridor_version = registry.version;
  return corridor;
}

export const api = {
  async vehicles() {
    const vehicles = Object.entries(VEHICLE_DB).map(([name, spec]) => ({
      name, battery_kwh: spec.battery_kwh, efficiency_wh_km: spec.efficiency_wh_km,
      claimed: spec.claimed_range, claimed_km: claimedKm(spec.claimed_range),
      drag_coefficient: spec.drag_coefficient, frontal_area_m2: spec.frontal_area_m2,
      mass_kg: spec.mass_kg, mre_pct: spec.mre_pct, rolling_resistance: spec.rolling_resistance,
    }));
    vehicles.sort((a, b) => {
      const ac = a.name.startsWith('Custom'), bc = b.name.startsWith('Custom');
      if (ac !== bc) return ac ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
    return { vehicles, driver_styles: Object.keys(DRIVER_STYLE_MULTIPLIERS) };
  },

  async status() {
    const registry = await loadRegistry();
    return {
      chargers: registry.chargers.length, registry_last_updated: registry.registryLastUpdated,
      health_checked: registry.healthCheckedCount, flagged_down: registry.downCount, auto_refresh: true,
    };
  },

  async geocode(q) {
    if (!q || q.trim().length < 2) return { results: [] };
    let data;
    try {
      const params = new URLSearchParams({
        f: 'json', singleLine: q, sourceCountry: 'IND', maxLocations: 5,
        outFields: 'Region,City', category: 'Populated Place,City,Town,Village,POI,Point Address,Street Address',
      });
      data = await fetchJsonWithTimeout(`${GEOCODE_URL}/findAddressCandidates?${params}`, 8000);
    } catch {
      throw new Error('Place search is unavailable right now.');
    }
    const results = [], seen = new Set();
    for (const c of data.candidates || []) {
      const loc = c.location || {};
      const label = (c.address || '').trim();
      if (!label || seen.has(label) || (c.score || 0) < 60) continue;
      const lat = loc.y || 0, lng = loc.x || 0;
      if (!inIndia(lat, lng)) continue;
      seen.add(label);
      results.push({ label, lat, lng });
    }
    return { results };
  },

  async reverse(lat, lng) {
    if (!inIndia(lat, lng)) throw new Error('Marga plans trips within India.');
    try {
      const params = new URLSearchParams({ f: 'json', location: `${lng},${lat}` });
      const data = await fetchJsonWithTimeout(`${GEOCODE_URL}/reverseGeocode?${params}`, 6000);
      const addr = data.address || {};
      const label = addr.City || addr.Neighborhood || addr.Match_addr || `${lat.toFixed(3)}, ${lng.toFixed(3)}`;
      return { label, lat, lng };
    } catch {
      return { label: `${lat.toFixed(3)}, ${lng.toFixed(3)}`, lat, lng };
    }
  },

  async routes(start, end) {
    if (!(inIndia(start.lat, start.lng) && inIndia(end.lat, end.lng))) {
      throw new Error('Both places need to be within India.');
    }
    const routes = await getOsrmRoute([start.lat, start.lng], [end.lat, end.lng]);
    const out = [];
    for (let i = 0; i < routes.length; i++) {
      const r = routes[i];
      const id = `r${++routeSeq}`;
      routeStore.set(id, { geometry: r.geometry, duration_mins: r.duration_mins, distance_km: r.distance_km });
      if (routeStore.size > 20) routeStore.delete(routeStore.keys().next().value);

      const step = Math.max(1, Math.floor(r.geometry.length / 5000));
      const drawn = [];
      for (let j = 0; j < r.geometry.length; j += step) drawn.push([round5(r.geometry[j][1]), round5(r.geometry[j][0])]);
      const lastPt = r.geometry[r.geometry.length - 1];
      const lastDrawn = [round5(lastPt[1]), round5(lastPt[0])];
      if (drawn[drawn.length - 1][0] !== lastDrawn[0] || drawn[drawn.length - 1][1] !== lastDrawn[1]) drawn.push(lastDrawn);

      out.push({ id, label: `Route ${i + 1}`, distance_km: r.distance_km, duration_mins: r.duration_mins, fallback: r.fallback, geometry: drawn });
    }
    return { routes: out };
  },

  async plan(payload) {
    if (!(payload.driver_style in DRIVER_STYLE_MULTIPLIERS)) throw new Error('Unknown driving style.');
    const entry = routeStore.get(payload.route_id);
    if (!entry) throw new Error('That route expired. Search again to refresh it.');

    const v = payload.vehicle;
    const spec = {
      battery_kwh: v.battery_kwh, efficiency_wh_km: v.efficiency_wh_km, drag_coefficient: v.drag_coefficient,
      frontal_area_m2: v.frontal_area_m2, mass_kg: v.mass_kg, mre_pct: v.mre_pct, rolling_resistance: v.rolling_resistance,
    };

    if (!entry.elevation) {
      const [elevation, weather] = await Promise.all([
        getElevationProfile(entry.geometry), sampleWeatherAlongRoute(entry.geometry),
      ]);
      entry.elevation = elevation;
      entry.weather = weather;
    }
    const { elevation, weather } = entry;

    const registry = await loadRegistry();
    const corridor = corridorChargers(entry, registry);
    if (!corridor.length) throw new Error('No fast chargers are listed along this route yet.');

    const geometry = entry.geometry;
    const durationH = Math.max(entry.duration_mins, 1) / 60.0;
    const avgSpeed = Math.min(110.0, Math.max(30.0, entry.distance_km / durationH));

    const sim = simulateTrip(geometry, spec, payload.safety_buffer_pct, payload.avoid_down, {
      driver_style: payload.driver_style, pre_fetched_chargers: corridor, avg_speed_kmh: avgSpeed,
      elevation_profile: elevation, weather_samples: weather, start_soc_pct: payload.start_soc_pct,
    });

    const batteryWh = spec.battery_kwh * 1000.0;
    const totalKm = sim.total_distance;
    const physicsRange = sim.avg_wh_per_km > 0 ? batteryWh / sim.avg_wh_per_km : flatBaselineRangeKm(spec);
    const baselineRange = flatBaselineRangeKm(spec);

    let claimedRangeKm = null;
    const known = VEHICLE_DB[v.name];
    if (known) claimedRangeKm = claimedKm(known.claimed_range);

    const wxTemps = weather.map((s) => s.weather.temperature);
    const wxWinds = weather.map((s) => s.weather.wind_speed_kmh);
    if (!wxTemps.length) wxTemps.push(28.0);
    if (!wxWinds.length) wxWinds.push(0.0);
    const mid = weather.length ? weather[Math.floor(weather.length / 2)].weather : { description: 'clear', temperature: 28.0 };
    const elevationChange = elevation.length ? elevation[elevation.length - 1] - elevation[0] : 0.0;
    const weatherOut = {
      description: mid.description || 'clear', temp_min: Math.min(...wxTemps), temp_max: Math.max(...wxTemps),
      wind_kmh: Math.max(...wxWinds), elevation_change_m: elevationChange,
      elevation_min_m: elevation.length ? Math.min(...elevation) : 0.0,
      elevation_max_m: elevation.length ? Math.max(...elevation) : 0.0,
    };

    const explanation = explainRangeAdjustment(baselineRange, physicsRange, mid, elevationChange, payload.driver_style);

    const stopsOut = [];
    let chargeTotal = 0.0;
    const driveMinsTotal = entry.duration_mins;
    sim.stops.forEach((s, i) => {
      const brief = chargerBrief(s.charger);
      chargeTotal += s.charge_minutes;
      stopsOut.push({
        ...brief, index: i + 1, at_km: s.stopped_at_km, detour_km: s.detour_km,
        arrival_soc_pct: (s.arrival_wh / batteryWh) * 100.0, depart_soc_pct: (s.depart_wh / batteryWh) * 100.0,
        charge_minutes: s.charge_minutes, note: s.note,
        drive_eta_min: totalKm ? (s.stopped_at_km / totalKm) * driveMinsTotal : 0.0,
        backup: chargerBrief(s.backup_charger), backup_gap_km: s.backup_gap_km,
      });
    });
    let running = 0.0;
    for (const st of stopsOut) { st.eta_min = st.drive_eta_min + running; running += st.charge_minutes; delete st.drive_eta_min; }

    let profile = null;
    if (sim.status === 'success' || sim.legs) {
      const n = geometry.length;
      const count = Math.min(240, n);
      const idxSet = new Set();
      for (let k = 0; k < count; k++) idxSet.add(Math.round((k * (n - 1)) / Math.max(1, count - 1)));
      const idxs = [...idxSet].sort((a, b) => a - b);
      const legs = sim.legs, kmArr = sim.km_by_point, enArr = sim.energy_wh_by_point;
      const kmL = [], elevL = [], socL = [];
      let li = 0;
      for (const i of idxs) {
        while (li + 1 < legs.length && legs[li + 1].start_idx <= i) li++;
        const soc = legs[li].start_soc - (enArr[i] - legs[li].start_wh);
        kmL.push(round2(kmArr[i]));
        elevL.push(elevation.length ? round1(elevation[i]) : 0.0);
        socL.push(round2(Math.max(-5.0, (soc / batteryWh) * 100.0)));
      }
      profile = { km: kmL, elev: elevL, soc: socL };
    }

    const factors = {};
    for (const [k, val] of Object.entries(sim.factor_wh)) factors[k] = val / 1000.0;

    const corridorOut = corridor.slice(0, 600).map((c) => ({
      lat: c.AddressInfo.Latitude, lng: c.AddressInfo.Longitude, kw: c.max_ccs2_power || 0, ok: c.is_working !== false,
    }));

    return {
      status: sim.status, message: sim.message,
      summary: {
        distance_km: totalKm, drive_mins: driveMinsTotal, charge_mins: chargeTotal, total_mins: driveMinsTotal + chargeTotal,
        physics_range_km: physicsRange, baseline_range_km: baselineRange, claimed_range_km: claimedRangeKm,
        avg_wh_km: sim.avg_wh_per_km, avg_speed_kmh: avgSpeed, arrival_soc_pct: sim.arrival_soc_pct,
        trip_energy_kwh: sim.total_energy_wh / 1000.0, stops: stopsOut.length,
      },
      factors_kwh: factors, weather: weatherOut, explanation, stops: stopsOut, profile, chargers: corridorOut,
      registry: { last_updated: registry.registryLastUpdated, health_checked: registry.healthCheckedCount, flagged_down: registry.downCount },
    };
  },
};
