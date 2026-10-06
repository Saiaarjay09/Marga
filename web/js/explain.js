// Plain-English range explanation, ported from core/ai_optimizer.py's
// deterministic fallback. The LLM-narration path isn't ported here: Gemini
// needs a secret key, which client-side code has nowhere safe to hold, and
// no key was ever configured in production, so this fallback sentence is
// exactly what was already being shown.

export function explainRangeAdjustment(baselineRangeKm, physicsRangeKm, weather, elevationChangeM, driverStyle) {
  const deltaPct = baselineRangeKm ? ((physicsRangeKm - baselineRangeKm) / baselineRangeKm) * 100.0 : 0.0;
  const direction = deltaPct < 0 ? 'lower' : 'higher';
  const temp = weather.temperature ?? weather.temp ?? 28;
  const sign = elevationChangeM >= 0 ? '+' : '';
  return (
    `Physics model estimates ${Math.round(physicsRangeKm)} km (${Math.abs(Math.round(deltaPct))}% ${direction} than the ` +
    `${Math.round(baselineRangeKm)} km baseline) given ${weather.description || 'current'} conditions at ` +
    `${temp}°C, a ${sign}${Math.round(elevationChangeM)} m elevation change, and ${driverStyle.toLowerCase()} driving style.`
  );
}
