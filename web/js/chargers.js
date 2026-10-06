// Loads the charger registry and its health overlay as static JSON (same
// files server.py serves, now under web/data/ so GitHub Pages can publish
// them too), ported from core/charger_registry.py + server.py's
// RegistryCache. Fetched once per page load and cached in memory; a fresh
// visit always gets whatever the last daily refresh committed.

const REGISTRY_URL = 'data/every_charger_india.json';
const HEALTH_URL = 'data/charger_health.json';
const MIN_CHARGER_KW = 25;
const TWO_WHEELER_NETWORKS = ['ather', 'ola', 'revolt', 'bounce', 'yulu', 'e:swap', 'tvs', 'honda'];

async function fetchJson(url) {
  try {
    const res = await fetch(url, { cache: 'no-cache' });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

let cached = null;

export async function loadRegistry() {
  if (cached) return cached;

  const [registryRaw, health] = await Promise.all([fetchJson(REGISTRY_URL), fetchJson(HEALTH_URL)]);
  const rawChargers = Array.isArray(registryRaw) ? registryRaw : (registryRaw?.chargers || []);
  const registryLastUpdated = Array.isArray(registryRaw) ? null : (registryRaw?.last_updated ?? null);
  const healthMap = health || {};

  const chargers = [];
  for (const charger of rawChargers) {
    const record = healthMap[String(charger.ID)];
    if (record) {
      charger.is_working = record.is_working ?? true;
      charger.health_confidence = record.confidence ?? 50;
      charger.health_reason = record.reason ?? '';
      charger.health_last_checked = record.last_checked ?? null;
      charger.consecutive_failures = record.consecutive_failures ?? 0;
    } else {
      charger.is_working = true; // unchecked stations default to "assume working"
      charger.health_confidence = 50;
      charger.health_reason = 'Not yet health-checked.';
      charger.health_last_checked = null;
      charger.consecutive_failures = 0;
    }

    const operator = ((charger.OperatorInfo || {}).Title || '').toLowerCase();
    if (TWO_WHEELER_NETWORKS.some((n) => operator.includes(n))) continue;

    const conns = charger.Connections || [];
    const power = conns.reduce((max, c) => Math.max(max, c.PowerKW || 0), 0);
    if (power < MIN_CHARGER_KW) continue;
    charger.max_ccs2_power = power;

    chargers.push(charger);
  }

  const downCount = chargers.reduce((n, c) => n + (c.is_working ? 0 : 1), 0);
  cached = {
    chargers, registryLastUpdated,
    healthCheckedCount: Object.keys(healthMap).length,
    downCount, version: 1,
  };
  return cached;
}
