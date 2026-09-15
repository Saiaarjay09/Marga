"""
Optional narrative layer. All of the actual range math now lives in
core.physics + core.simulator (deterministic and reproducible); this class
only asks an LLM to explain, in plain English, why the physics model came
out the way it did. It never sets the range number itself anymore -- the
old version had the opposite design (an LLM guessing a raw efficiency
percentage), which meant the headline number could vary between two runs
of the same trip.
"""

from typing import Any, Dict, Optional

try:
    from google import genai
except ImportError:
    genai = None


class AIOptimizer:
    def __init__(self, gemini_api_key: Optional[str] = None):
        self.client = None
        if gemini_api_key and genai:
            try:
                self.client = genai.Client(api_key=gemini_api_key)
            except Exception:
                self.client = None

    def explain_range_adjustment(self, baseline_range_km: float, physics_range_km: float,
                                  weather: Dict[str, Any], elevation_change_m: float,
                                  driver_style: str) -> str:
        """Returns a plain-English explanation of why the physics-adjusted range differs from the claimed baseline."""
        delta_pct = ((physics_range_km - baseline_range_km) / baseline_range_km * 100.0) if baseline_range_km else 0.0

        if self.client:
            prompt = f"""
            You are explaining an EV range estimate to a driver in one or two sentences.
            Manufacturer-style baseline range: {baseline_range_km:.0f} km
            Physics-model estimate for this specific trip: {physics_range_km:.0f} km ({delta_pct:+.1f}%)
            Weather: {weather.get('description', 'clear')}, {weather.get('temperature', weather.get('temp', 28))}°C,
              wind {weather.get('wind_speed_kmh', weather.get('wind_speed', 12)):.0f} km/h
            Net elevation change: {elevation_change_m:.0f} m
            Driver style: {driver_style}

            Respond with ONLY the one-or-two sentence explanation, no markdown, no JSON.
            """
            try:
                response = self.client.models.generate_content(model="gemini-2.5-flash-lite", contents=prompt)
                text = response.text.strip()
                if text:
                    return text
            except Exception as e:
                print(f"LLM narration failed, using fallback: {e}")

        direction = "lower" if delta_pct < 0 else "higher"
        return (
            f"Physics model estimates {physics_range_km:.0f} km ({abs(delta_pct):.0f}% {direction} than the "
            f"{baseline_range_km:.0f} km baseline) given {weather.get('description', 'current')} conditions at "
            f"{weather.get('temperature', weather.get('temp', 28))}°C, a {elevation_change_m:+.0f} m elevation "
            f"change, and {driver_style.lower()} driving style."
        )
