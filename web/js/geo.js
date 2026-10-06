// Geometry helpers, ported from core/geo.py. Field names and formulas match
// the Python source line for line so the two stay easy to cross-check.

export const EARTH_RADIUS_KM = 6371.0;

export function haversineKm(lat1, lon1, lat2, lon2) {
  const rad = Math.PI / 180;
  const dLat = (lat2 - lat1) * rad;
  const dLon = (lon2 - lon1) * rad;
  const a = Math.sin(dLat / 2) ** 2 + Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * EARTH_RADIUS_KM * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

/** Initial compass bearing (0-360, 0=N) travelling from point 1 to point 2. */
export function bearingDeg(lat1, lon1, lat2, lon2) {
  const rad = Math.PI / 180;
  const phi1 = lat1 * rad, phi2 = lat2 * rad, dLambda = (lon2 - lon1) * rad;
  const x = Math.sin(dLambda) * Math.cos(phi2);
  const y = Math.cos(phi1) * Math.sin(phi2) - Math.sin(phi1) * Math.cos(phi2) * Math.cos(dLambda);
  return (Math.atan2(x, y) / rad + 360) % 360;
}

/**
 * Index and distance (km) of the route point closest to (lat, lng).
 * routeLat/routeLng are plain arrays of the same length. A coarse pass every
 * ~n/400 points followed by a local refine keeps this fast on long routes
 * without the full O(n) haversine cost per charger.
 */
export function nearestRoutePoint(routeLat, routeLng, lat, lng) {
  const n = routeLat.length;
  if (n === 0) return [0, Infinity];
  const coarseStep = Math.max(1, Math.floor(n / 400));

  let bestIdx = 0, bestDist = Infinity;
  for (let i = 0; i < n; i += coarseStep) {
    const d = haversineKm(routeLat[i], routeLng[i], lat, lng);
    if (d < bestDist) { bestDist = d; bestIdx = i; }
  }
  const lo = Math.max(0, bestIdx - coarseStep), hi = Math.min(n - 1, bestIdx + coarseStep);
  for (let i = lo; i <= hi; i++) {
    const d = haversineKm(routeLat[i], routeLng[i], lat, lng);
    if (d < bestDist) { bestDist = d; bestIdx = i; }
  }
  return [bestIdx, bestDist];
}
