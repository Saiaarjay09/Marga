import requests
from typing import Dict, Any

def get_weather(lat: float, lon: float) -> Dict[str, Any]:
    """Fetches current weather from Open-Meteo API with strict 1.0s timeout."""
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true"
    try:
        response = requests.get(url, timeout=1.0)
        response.raise_for_status()
        data = response.json()
        current = data.get("current_weather", {})
        
        weathercode = current.get("weathercode", 0)
        if weathercode <= 3:
            description = "clear/partly cloudy"
        elif weathercode <= 48:
            description = "fog/cloudy"
        elif weathercode <= 69:
            description = "rain/drizzle"
        elif weathercode <= 79:
            description = "snow"
        else:
            description = "heavy rain/storm"
            
        return {
            "temperature": current.get("temperature", 28.0),
            "wind_speed": current.get("windspeed", 12.0),
            "weather_code": weathercode,
            # Also keeping the legacy keys for ai_optimizer.py compatibility
            "temp": current.get("temperature", 28.0),
            "description": description
        }
    except Exception:
        # Silently catch all timeouts and network exceptions
        return {
            "temperature": 28.0, 
            "wind_speed": 12.0, 
            "weather_code": 0,
            # Legacy keys to prevent ai_optimizer.py KeyError
            "temp": 28.0,
            "description": "clear"
        }
