// Driving routes via OSRM's public demo server, ported from api/routing.py,
// called directly from the browser (confirmed CORS-enabled). Falls back to a
// mirror, then to a straight-line interpolation if both are unreachable.

import { haversineKm } from './geo.js';

// Standard Google/OSRM encoded-polyline decoder (precision 5). Returns [lat, lng] pairs.
function decodePolyline(encoded) {
  const points = [];
  let index = 0, lat = 0, lng = 0;
  const len = encoded.length;
  while (index < len) {
    let b, shift = 0, result = 0;
    do { b = encoded.charCodeAt(index++) - 63; result |= (b & 0x1f) << shift; shift += 5; } while (b >= 0x20);
    lat += result & 1 ? ~(result >> 1) : result >> 1;
    shift = 0; result = 0;
    do { b = encoded.charCodeAt(index++) - 63; result |= (b & 0x1f) << shift; shift += 5; } while (b >= 0x20);
    lng += result & 1 ? ~(result >> 1) : result >> 1;
    points.push([lat / 1e5, lng / 1e5]);
  }
  return points;
}

function straightLineFallback(startLat, startLng, endLat, endLng) {
  const numPoints = 20;
  const geometry = [];
  for (let i = 0; i < numPoints; i++) {
    const frac = i / (numPoints - 1);
    geometry.push([startLng + (endLng - startLng) * frac, startLat + (endLat - startLat) * frac]);
  }
  const distanceKm = haversineKm(startLat, startLng, endLat, endLng);
  return { geometry, distance_km: Math.round(distanceKm * 10) / 10, duration_mins: Math.round(distanceKm), fallback: true };
}

async function fetchWithTimeout(url, ms) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), ms);
  try {
    return await fetch(url, { signal: ctrl.signal });
  } finally {
    clearTimeout(timer);
  }
}

/**
 * startCoords/endCoords: [lat, lng]. Returns up to 3 route alternatives, each
 * { distance_km, duration_mins, geometry: [[lng, lat], ...], fallback }.
 */
export async function getOsrmRoute(startCoords, endCoords) {
  const [startLat, startLng] = startCoords, [endLat, endLng] = endCoords;
  const coordsString = `${startLng},${startLat};${endLng},${endLat}`;
  const query = 'overview=full&geometries=polyline&alternatives=3';
  const endpoints = [
    `https://router.project-osrm.org/route/v1/driving/${coordsString}?${query}`,
    `https://routing.openstreetmap.de/routed-car/route/v1/driving/${coordsString}?${query}`,
  ];

  for (const url of endpoints) {
    try {
      const res = await fetchWithTimeout(url, 10000);
      if (!res.ok) continue;
      const data = await res.json();
      if (data.code === 'Ok' && (data.routes || []).length > 0) {
        return data.routes.map((route) => {
          const decoded = decodePolyline(route.geometry); // [lat, lng]
          const geometry = decoded.map(([lat, lng]) => [lng, lat]); // -> [lng, lat]
          return {
            distance_km: Math.round((route.distance / 1000.0) * 10) / 10,
            duration_mins: Math.round(route.duration / 60.0),
            geometry, fallback: false,
          };
        });
      }
    } catch {
      // try the next endpoint
    }
  }

  console.warn('All routing endpoints failed; falling back to a straight-line estimate.');
  return [straightLineFallback(startLat, startLng, endLat, endLng)];
}
