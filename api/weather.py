"""
Weather for the range model.

Uses OpenWeatherMap when an API key is available (more reliable, includes
wind direction and a proper condition code); falls back to the free,
keyless Open-Meteo API otherwise. Both return wind direction as the
meteorological "blowing from" bearing in degrees, which physics.py's
headwind projection expects.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Sequence

import requests

OWM_URL = "https://api.openweathermap.org/data/2.5/weather"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

DEFAULT_WEATHER = {
    "temperature": 28.0,
    "wind_speed_kmh": 12.0,
    "wind_deg": 0.0,
    "weather_code": 0,
    "temp": 28.0,
    "description": "clear",
}


def _describe_weather_code(code: int) -> str:
    if code <= 3:
        return "clear/partly cloudy"
    if code <= 48:
        return "fog/cloudy"
    if code <= 69:
        return "rain/drizzle"
    if code <= 79:
        return "snow"
    return "heavy rain/storm"


def _from_open_meteo(lat: float, lon: float) -> Dict[str, Any]:
    resp = requests.get(OPEN_METEO_URL, params={
        "latitude": lat, "longitude": lon, "current_weather": True,
    }, timeout=3.0)
    resp.raise_for_status()
    current = resp.json().get("current_weather", {})
    weathercode = current.get("weathercode", 0)
    temp = current.get("temperature", DEFAULT_WEATHER["temperature"])
    return {
        "temperature": temp,
        "wind_speed_kmh": current.get("windspeed", DEFAULT_WEATHER["wind_speed_kmh"]),
        "wind_deg": current.get("winddirection", 0.0),
        "weather_code": weathercode,
        "temp": temp,
        "description": _describe_weather_code(weathercode),
    }


def _from_openweathermap(lat: float, lon: float, api_key: str) -> Dict[str, Any]:
    resp = requests.get(OWM_URL, params={
        "lat": lat, "lon": lon, "appid": api_key, "units": "metric",
    }, timeout=3.0)
    resp.raise_for_status()
    data = resp.json()
    temp = data.get("main", {}).get("temp", DEFAULT_WEATHER["temperature"])
    wind = data.get("wind", {})
    description = (data.get("weather") or [{}])[0].get("description", "clear")
    return {
        "temperature": temp,
        "wind_speed_kmh": wind.get("speed", 3.3) * 3.6,
        "wind_deg": wind.get("deg", 0.0),
        "weather_code": (data.get("weather") or [{}])[0].get("id", 800),
        "temp": temp,
        "description": description,
    }


def get_weather(lat: float, lon: float, api_key: str = None) -> Dict[str, Any]:
    """Fetches current weather, preferring OpenWeatherMap when a key is supplied."""
    if api_key:
        try:
            return _from_openweathermap(lat, lon, api_key)
        except Exception:
            pass
    try:
        return _from_open_meteo(lat, lon)
    except Exception:
        return dict(DEFAULT_WEATHER)


def headwind_component_kmh(wind_speed_kmh: float, wind_from_deg: float, travel_bearing_deg: float) -> float:
    """
    Positive = headwind (slows you down), negative = tailwind (helps you).
    wind_from_deg is the meteorological bearing the wind blows FROM.
    """
    angle = math.radians(wind_from_deg - travel_bearing_deg)
    return wind_speed_kmh * math.cos(angle)


def sample_weather_along_route(route_geometry: Sequence[Sequence[float]], api_key: str = None,
                                max_samples: int = 6) -> List[Dict[str, Any]]:
    """
    Returns up to max_samples weather readings evenly spaced along the route,
    each tagged with the [lng, lat] index it was taken at, for interpolation
    onto route segments.
    """
    n = len(route_geometry)
    if n == 0:
        return []
    if n <= max_samples:
        indices = list(range(n))
    else:
        indices = [round(i * (n - 1) / (max_samples - 1)) for i in range(max_samples)]

    samples = []
    for idx in sorted(set(indices)):
        lng, lat = route_geometry[idx][0], route_geometry[idx][1]
        weather = get_weather(lat, lng, api_key)
        samples.append({"index": idx, "weather": weather})
    return samples
