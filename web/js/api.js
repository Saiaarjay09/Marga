// Thin API client. Paths are relative so the site works at "/" and at "/marga/".

async function request(path, options = {}) {
  let res;
  try {
    res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
  } catch {
    throw new Error("Can't reach Marga right now. Check your connection and try again.");
  }
  let body = null;
  try { body = await res.json(); } catch { /* non-JSON error page */ }
  if (!res.ok) {
    const detail = body && (typeof body.detail === 'string' ? body.detail : null);
    throw new Error(detail || `Something went wrong (${res.status}).`);
  }
  return body;
}

export const api = {
  vehicles: () => request('api/vehicles'),
  status: () => request('api/status'),
  geocode: (q) => request(`api/geocode?q=${encodeURIComponent(q)}`),
  reverse: (lat, lng) => request(`api/reverse?lat=${lat}&lng=${lng}`),
  routes: (start, end) => request('api/routes', { method: 'POST', body: JSON.stringify({ start, end }) }),
  plan: (payload) => request('api/plan', { method: 'POST', body: JSON.stringify(payload) }),
};
