// Physics-based EV energy model, ported from core/physics.py. See that file
// for the full explanation of the force-balance model; this is a line-for-line
// JS port so the client-side (GitHub Pages) and server-side (self-hosted)
// deployments compute identical numbers.

export const G = 9.81;
export const RHO_SEA_LEVEL = 1.225;
export const SCALE_HEIGHT_M = 8434.0;
export const REGEN_EFFICIENCY = 0.65;
export const MAX_REGEN_POWER_W = 70_000.0;

export const DRIVER_STYLE_MULTIPLIERS = { Eco: 0.93, Normal: 1.00, Aggressive: 1.14 };

// cond: { distance_km, avg_speed_kmh, elevation_gain_m, altitude_m, headwind_kmh, ambient_temp_c }

export function airDensity(altitudeM) {
  return RHO_SEA_LEVEL * Math.exp(-Math.max(altitudeM, 0.0) / SCALE_HEIGHT_M);
}

export function hvacLoadKw(ambientTempC) {
  const baseAccessoryKw = 0.30;
  if (ambientTempC < 15.0) return baseAccessoryKw + Math.min(6.0, 0.15 * (15.0 - ambientTempC));
  if (ambientTempC > 28.0) return baseAccessoryKw + Math.min(3.5, 0.25 * (ambientTempC - 28.0));
  return baseAccessoryKw;
}

/**
 * Battery-side energy (Wh) for one segment, split by cause. The parts always
 * sum to the segment total: aero, wind, rolling, hills, climate, style.
 */
export function segmentEnergyParts(spec, cond, driverStyle = 'Normal') {
  const parts = { aero: 0.0, wind: 0.0, rolling: 0.0, hills: 0.0, climate: 0.0, style: 0.0 };
  if (cond.distance_km <= 0) return parts;

  const distanceM = cond.distance_km * 1000.0;
  const vCar = cond.avg_speed_kmh / 3.6;
  const vRel = vCar + cond.headwind_kmh / 3.6;

  const rho = airDensity(cond.altitude_m);
  const kAero = 0.5 * rho * spec.drag_coefficient * spec.frontal_area_m2;
  const fAeroStill = kAero * vCar * vCar;
  const fAeroWind = kAero * Math.sign(vRel) * vRel * vRel - fAeroStill;

  const theta = Math.atan(cond.elevation_gain_m / distanceM);
  const fRoll = spec.rolling_resistance * spec.mass_kg * G * Math.cos(theta);
  const fGrade = spec.mass_kg * G * Math.sin(theta);

  const workWh = {
    aero: (fAeroStill * distanceM) / 3600.0,
    wind: (fAeroWind * distanceM) / 3600.0,
    rolling: (fRoll * distanceM) / 3600.0,
    hills: (fGrade * distanceM) / 3600.0,
  };
  const totalWorkWh = workWh.aero + workWh.wind + workWh.rolling + workWh.hills;
  const styleMultiplier = DRIVER_STYLE_MULTIPLIERS[driverStyle] ?? 1.0;

  if (totalWorkWh >= 0) {
    const scale = 1.0 / spec.mre_pct;
    for (const key of ['aero', 'wind', 'rolling', 'hills']) parts[key] = workWh[key] * scale;
    const baseSum = parts.aero + parts.wind + parts.rolling + parts.hills;
    parts.style = baseSum * (styleMultiplier - 1.0);
  } else {
    const timeS = vCar > 0 ? distanceM / vCar : 0.0;
    const powerW = timeS > 0 ? (totalWorkWh * 3600.0) / timeS : totalWorkWh * 3600.0;
    const clampedWh = timeS > 0 ? (Math.max(powerW, -MAX_REGEN_POWER_W) * timeS) / 3600.0 : totalWorkWh;
    const scale = (clampedWh * REGEN_EFFICIENCY) / totalWorkWh;
    for (const key of ['aero', 'wind', 'rolling', 'hills']) parts[key] = workWh[key] * scale;
  }

  const timeHours = cond.avg_speed_kmh > 0 ? cond.distance_km / cond.avg_speed_kmh : 0.0;
  parts.climate = hvacLoadKw(cond.ambient_temp_c) * 1000.0 * timeHours;
  return parts;
}

export function segmentEnergyWh(spec, cond, driverStyle = 'Normal') {
  const parts = segmentEnergyParts(spec, cond, driverStyle);
  return parts.aero + parts.wind + parts.rolling + parts.hills + parts.climate + parts.style;
}

export function flatBaselineRangeKm(spec) {
  return (spec.battery_kwh * 1000.0) / spec.efficiency_wh_km;
}
