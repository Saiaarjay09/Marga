// Weather for the range model, ported from api/weather.py. The static
// (GitHub Pages) build has nowhere safe to hold an OpenWeatherMap key in
// client code, so this always uses Open-Meteo, which needs no key and
// already was the actual fallback in production (no OWM key was ever
// configured). Wind direction is the meteorological "blowing from" bearing,
// which physics.js's headwind projection expects.

const OPEN_METEO_URL = 'https://api.open-meteo.com/v1/forecast';
const DEFAULT_WEATHER = {
  temperature: 28.0, wind_speed_kmh: 12.0, wind_deg: 0.0,
  weather_code: 0, temp: 28.0, description: 'clear',
};

function describeWeatherCode(code) {
  if (code <= 3) return 'clear/partly cloudy';
  if (code <= 48) return 'fog/cloudy';
  if (code <= 69) return 'rain/drizzle';
  if (code <= 79) return 'snow';
  return 'heavy rain/storm';
}

export async function getWeather(lat, lon) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 4000);
  try {
    const url = `${OPEN_METEO_URL}?latitude=${lat}&longitude=${lon}&current_weather=true`;
    const res = await fetch(url, { signal: ctrl.signal });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const current = data.current_weather || {};
    const code = current.weathercode ?? 0;
    const temp = current.temperature ?? DEFAULT_WEATHER.temperature;
    return {
      temperature: temp,
      wind_speed_kmh: current.windspeed ?? DEFAULT_WEATHER.wind_speed_kmh,
      wind_deg: current.winddirection ?? 0.0,
      weather_code: code, temp, description: describeWeatherCode(code),
    };
  } catch {
    return { ...DEFAULT_WEATHER };
  } finally {
    clearTimeout(timer);
  }
}

/** Positive = headwind (slows you down), negative = tailwind (helps). */
export function headwindComponentKmh(windSpeedKmh, windFromDeg, travelBearingDeg) {
  const angle = ((windFromDeg - travelBearingDeg) * Math.PI) / 180;
  return windSpeedKmh * Math.cos(angle);
}

/**
 * Up to maxSamples weather readings evenly spaced along the route, each
 * tagged with the route-point index it was taken at, for interpolation onto
 * route segments. Fetched in parallel (the Python original did this
 * sequentially server-side to be polite to a shared IP; a browser calling on
 * the user's own connection has no such need).
 */
export async function sampleWeatherAlongRoute(routeGeometry, maxSamples = 6) {
  const n = routeGeometry.length;
  if (n === 0) return [];
  let indices;
  if (n <= maxSamples) indices = Array.from({ length: n }, (_, i) => i);
  else indices = Array.from({ length: maxSamples }, (_, i) => Math.round((i * (n - 1)) / (maxSamples - 1)));

  const unique = [...new Set(indices)].sort((a, b) => a - b);
  return Promise.all(unique.map(async (idx) => {
    const [lng, lat] = routeGeometry[idx];
    const weather = await getWeather(lat, lng);
    return { index: idx, weather };
  }));
}
