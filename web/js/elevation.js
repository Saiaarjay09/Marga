// Route elevation profile via the free, keyless Open-Meteo Elevation API,
// ported from core/elevation.py. Samples the route every ~2km (up to 100
// points per request) and linearly interpolates elevation for every point
// in between.

import { haversineKm } from './geo.js';

const ELEVATION_URL = 'https://api.open-meteo.com/v1/elevation';
const MAX_POINTS_PER_REQUEST = 100;
const SAMPLE_INTERVAL_KM = 2.0;

async function fetchElevations(coords) {
  const elevations = [];
  for (let i = 0; i < coords.length; i += MAX_POINTS_PER_REQUEST) {
    const chunk = coords.slice(i, i + MAX_POINTS_PER_REQUEST);
    const latStr = chunk.map(([lat]) => lat.toFixed(5)).join(',');
    const lonStr = chunk.map(([, lon]) => lon.toFixed(5)).join(',');
    try {
      const res = await fetch(`${ELEVATION_URL}?latitude=${latStr}&longitude=${lonStr}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const elev = data.elevation || new Array(chunk.length).fill(0.0);
      for (const e of elev) elevations.push(Number(e));
    } catch {
      for (let k = 0; k < chunk.length; k++) elevations.push(0.0);
    }
  }
  return elevations;
}

/**
 * routeGeometry: array of [lng, lat] (or [lng, lat, ele]) points.
 * Returns an array of elevations in meters, one per input point.
 */
export async function getElevationProfile(routeGeometry) {
  const n = routeGeometry.length;
  if (n === 0) return [];
  if (n === 1) {
    const elev = await fetchElevations([[routeGeometry[0][1], routeGeometry[0][0]]]);
    return elev;
  }

  const cumulativeKm = [0.0];
  for (let i = 1; i < n; i++) {
    const prev = routeGeometry[i - 1], cur = routeGeometry[i];
    cumulativeKm.push(cumulativeKm[i - 1] + haversineKm(prev[1], prev[0], cur[1], cur[0]));
  }
  const totalKm = cumulativeKm[cumulativeKm.length - 1];

  if (totalKm <= 0) {
    const elev = await fetchElevations([[routeGeometry[0][1], routeGeometry[0][0]]]);
    return new Array(n).fill(elev.length ? elev[0] : 0.0);
  }

  const numSamples = Math.min(MAX_POINTS_PER_REQUEST, Math.max(2, Math.floor(totalKm / SAMPLE_INTERVAL_KM) + 1));
  const sampleTargetKms = Array.from({ length: numSamples }, (_, i) => (totalKm * i) / (numSamples - 1));

  const sampleIndices = [];
  let cursor = 0;
  for (const target of sampleTargetKms) {
    while (cursor < n - 1 && cumulativeKm[cursor] < target) cursor++;
    sampleIndices.push(cursor);
  }

  const sampleCoords = sampleIndices.map((idx) => [routeGeometry[idx][1], routeGeometry[idx][0]]);
  const sampleElevations = await fetchElevations(sampleCoords);
  const sampleKms = sampleIndices.map((idx) => cumulativeKm[idx]);

  const profile = [];
  let seg = 0;
  for (const km of cumulativeKm) {
    while (seg < sampleKms.length - 2 && km > sampleKms[seg + 1]) seg++;
    const k0 = sampleKms[seg], k1 = sampleKms[seg + 1];
    const e0 = sampleElevations[seg], e1 = sampleElevations[seg + 1];
    if (k1 === k0) profile.push(e0);
    else profile.push(e0 + (e1 - e0) * ((km - k0) / (k1 - k0)));
  }
  return profile;
}
