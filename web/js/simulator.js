// Trip simulator, ported from core/simulator.py. Walks the route in energy
// space (Wh) using physics.js for per-segment consumption, and the charger
// health overlay to avoid stations currently flagged down. Every stop gets
// a nearby backup. Field names deliberately mirror the Python dict keys
// (snake_case) so the two implementations stay easy to diff against each other.

import { bearingDeg, haversineKm, nearestRoutePoint } from './geo.js';
import { segmentEnergyParts } from './physics.js';

const DEFAULT_WEATHER = { temperature: 28.0, wind_speed_kmh: 12.0, wind_deg: 0.0 };
const BACKUP_MAX_GAP_KM = 60.0;
const CHARGE_TARGET = 0.85;
const PLUG_OVERHEAD_MIN = 5.0;
const EFFECTIVE_POWER_FACTOR = 0.72;
const VEHICLE_ACCEPT_CAP_KW = 150.0;

function nearestWeather(weatherSamples, pointIndex) {
  if (!weatherSamples || !weatherSamples.length) return DEFAULT_WEATHER;
  let best = weatherSamples[0], bestDiff = Infinity;
  for (const s of weatherSamples) {
    const diff = Math.abs(s.index - pointIndex);
    if (diff < bestDiff) { bestDiff = diff; best = s; }
  }
  return best.weather || DEFAULT_WEATHER;
}

function headwindKmh(windSpeedKmh, windFromDeg, travelBearingDeg) {
  return windSpeedKmh * Math.cos(((windFromDeg - travelBearingDeg) * Math.PI) / 180);
}

function buildEnergyProfile(routeGeometry, vehicle, avgSpeedKmh, driverStyle, elevationProfile, weatherSamples) {
  const n = routeGeometry.length;
  const physicalKm = new Array(n).fill(0.0);
  const energyWh = new Array(n).fill(0.0);
  const totals = { aero: 0.0, wind: 0.0, rolling: 0.0, hills: 0.0, climate: 0.0, style: 0.0 };

  for (let i = 1; i < n; i++) {
    const [lon1, lat1] = routeGeometry[i - 1], [lon2, lat2] = routeGeometry[i];
    const distKm = haversineKm(lat1, lon1, lat2, lon2);
    physicalKm[i] = physicalKm[i - 1] + distKm;

    const elev1 = elevationProfile ? elevationProfile[i - 1] : 0.0;
    const elev2 = elevationProfile ? elevationProfile[i] : 0.0;
    const weather = nearestWeather(weatherSamples, i);
    const bearing = bearingDeg(lat1, lon1, lat2, lon2);
    const headwind = headwindKmh(weather.wind_speed_kmh ?? 12.0, weather.wind_deg ?? 0.0, bearing);

    const cond = {
      distance_km: distKm, avg_speed_kmh: avgSpeedKmh, elevation_gain_m: elev2 - elev1,
      altitude_m: (elev1 + elev2) / 2.0, headwind_kmh: headwind, ambient_temp_c: weather.temperature ?? 28.0,
    };
    const parts = segmentEnergyParts(vehicle, cond, driverStyle);
    totals.aero += parts.aero; totals.wind += parts.wind; totals.rolling += parts.rolling;
    totals.hills += parts.hills; totals.climate += parts.climate; totals.style += parts.style;
    energyWh[i] = energyWh[i - 1] + parts.aero + parts.wind + parts.rolling + parts.hills + parts.climate + parts.style;
  }
  return { physical_km: physicalKm, energy_wh: energyWh, totals };
}

function projectChargers(chargers, routeGeometry, physicalKm, energyWh, vehicle) {
  const routeLat = routeGeometry.map((p) => p[1]);
  const routeLng = routeGeometry.map((p) => p[0]);
  const projections = [];
  for (const c of chargers) {
    const addr = c.AddressInfo || {};
    const cLat = addr.Latitude, cLng = addr.Longitude;
    if (cLat == null || cLng == null) continue;
    const [idx, offKm] = nearestRoutePoint(routeLat, routeLng, cLat, cLng);
    projections.push({
      charger: c, route_idx: idx, route_km: physicalKm[idx], route_energy_wh: energyWh[idx],
      dist_off_route_km: offKm, detour_wh: offKm * 2.0 * vehicle.efficiency_wh_km,
      power_kw: c.max_ccs2_power || 0, is_working: c.is_working !== false,
    });
  }
  return projections;
}

function chargeMinutes(energyWh, powerKw) {
  const effectiveKw = Math.max(10.0, Math.min(powerKw, VEHICLE_ACCEPT_CAP_KW) * EFFECTIVE_POWER_FACTOR);
  return PLUG_OVERHEAD_MIN + ((energyWh / 1000.0) / effectiveKw) * 60.0;
}

function finalizeStops(stops) {
  for (const s of stops) s.charge_minutes = chargeMinutes(Math.max(s.depart_wh - s.arrival_wh, 0.0), s.power_kw);
}

/**
 * route_geometry: [[lng, lat], ...]. vehicle: { battery_kwh, efficiency_wh_km,
 * drag_coefficient, frontal_area_m2, mass_kg, mre_pct, rolling_resistance }.
 */
export function simulateTrip(routeGeometry, vehicle, safetyBufferPct, reliabilityToggle, opts = {}) {
  const {
    driver_style = 'Normal', pre_fetched_chargers = [], avg_speed_kmh = 60.0,
    elevation_profile = null, weather_samples = null, start_soc_pct = 100.0,
  } = opts;

  if (!routeGeometry || !routeGeometry.length) return { status: 'error', message: 'Empty route', stops: [] };

  const { physical_km: physicalKm, energy_wh: energyWh, totals } =
    buildEnergyProfile(routeGeometry, vehicle, avg_speed_kmh, driver_style, elevation_profile, weather_samples);
  const totalKm = physicalKm[physicalKm.length - 1];
  const totalEnergyWh = energyWh[energyWh.length - 1];

  const batteryWh = vehicle.battery_kwh * 1000.0;
  const softWh = batteryWh * (safetyBufferPct / 100.0);
  const hardWh = softWh * 0.5;
  const startSocWh = (batteryWh * Math.max(5.0, Math.min(100.0, start_soc_pct))) / 100.0;

  const projections = projectChargers(pre_fetched_chargers || [], routeGeometry, physicalKm, energyWh, vehicle);

  let socWh = startSocWh, posWh = 0.0, posKm = 0.0;
  const stops = [];
  const legs = [{ start_idx: 0, start_wh: 0.0, start_soc: socWh }];

  const resultBase = {
    total_distance: totalKm, total_energy_wh: totalEnergyWh,
    avg_wh_per_km: totalKm ? totalEnergyWh / totalKm : 0.0,
    factor_wh: totals, energy_wh_by_point: energyWh, km_by_point: physicalKm,
  };

  let converged = false;
  for (let iter = 0; iter < 100; iter++) {
    if (posWh + socWh - softWh >= totalEnergyWh) { converged = true; break; }

    const candidates = [];
    for (const cp of projections) {
      if (cp.route_energy_wh <= posWh + 1.0) continue;
      const requiredWh = cp.route_energy_wh - posWh + cp.detour_wh / 2.0;
      const remainingWh = socWh - requiredWh;
      const isDcFast = cp.power_kw > 50;

      let tier;
      if (reliabilityToggle && !cp.is_working && remainingWh >= hardWh) tier = 'unhealthy';
      else if (remainingWh >= softWh) tier = 'standard';
      else if (remainingWh >= hardWh && remainingWh < softWh && isDcFast) tier = 'flex';
      else continue;
      candidates.push({ cp, remaining_wh: remainingWh, tier });
    }

    let pool = candidates.filter((c) => c.tier === 'standard' || c.tier === 'flex');
    if (!pool.length) pool = candidates.filter((c) => c.tier === 'unhealthy');
    if (!pool.length) {
      finalizeStops(stops);
      return {
        ...resultBase, status: 'failed', stops, legs,
        message: `No reachable charger ahead after ${Math.round(posKm)} km. Try a lower safety buffer, ` +
                 'a fuller start charge, or a different route.',
      };
    }

    const farKm = Math.max(...pool.map((c) => c.cp.route_km));
    let zone = pool.filter((c) => c.cp.route_km >= farKm - 30.0);
    if (!zone.length) zone = pool;

    const score = (c) => c.cp.power_kw * 10 + (c.cp.is_working ? 15 : -50) + c.cp.route_km;
    let best = zone[0];
    for (const c of zone) if (score(c) > score(best)) best = c;

    const bestId = best.cp.charger.ID;
    const poolTiers = new Set(pool.map((c) => c.tier));
    const backupOptions = candidates.filter(
      (c) => c.cp.charger.ID !== bestId && poolTiers.has(c.tier) && (c.cp.is_working || !best.cp.is_working)
    );
    let backup = null;
    if (backupOptions.length) {
      backup = backupOptions[0];
      for (const c of backupOptions) {
        if (Math.abs(c.cp.route_km - best.cp.route_km) < Math.abs(backup.cp.route_km - best.cp.route_km)) backup = c;
      }
      if (Math.abs(backup.cp.route_km - best.cp.route_km) > BACKUP_MAX_GAP_KM) backup = null;
    }

    const cp = best.cp;
    const arrivalWh = socWh - (cp.route_energy_wh - posWh) - cp.detour_wh / 2.0;
    const departWh = batteryWh * CHARGE_TARGET - cp.detour_wh / 2.0;
    let note = '';
    if (best.tier === 'unhealthy') note = 'Every reachable station nearby is flagged as possibly down; this is the best of them.';
    else if (best.tier === 'flex') note = 'Tight but safe: dips into your buffer to reach a fast charger.';

    stops.push({
      charger: cp.charger, backup_charger: backup ? backup.cp.charger : null,
      backup_gap_km: backup ? Math.abs(backup.cp.route_km - cp.route_km) : null,
      detour_km: cp.dist_off_route_km, power_kw: cp.power_kw, stopped_at_km: cp.route_km, route_idx: cp.route_idx,
      arrival_wh: arrivalWh, depart_wh: departWh, note, is_working: cp.is_working,
    });
    legs.push({ start_idx: cp.route_idx, start_wh: cp.route_energy_wh, start_soc: departWh });

    posWh = cp.route_energy_wh; posKm = cp.route_km; socWh = departWh;
  }

  if (!converged) {
    finalizeStops(stops);
    return { ...resultBase, status: 'failed', stops, legs, message: 'Route planning did not converge.' };
  }

  if (stops.length) {
    const last = stops[stops.length - 1];
    const detourHalf = last.detour_km * vehicle.efficiency_wh_km;
    const remaining = totalEnergyWh - energyWh[last.route_idx];
    const needed = remaining + softWh + batteryWh * 0.03 + detourHalf;
    last.depart_wh = Math.min(last.depart_wh, Math.max(needed, last.arrival_wh));
    legs[legs.length - 1].start_soc = last.depart_wh;
  }

  finalizeStops(stops);

  const finalLeg = legs[legs.length - 1];
  const arrivalSocWh = finalLeg.start_soc - (totalEnergyWh - finalLeg.start_wh);

  return {
    ...resultBase, status: 'success', message: 'Trip planned.', stops, legs,
    start_soc_wh: startSocWh, arrival_soc_pct: (arrivalSocWh / batteryWh) * 100.0,
  };
}
