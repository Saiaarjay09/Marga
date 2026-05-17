import json
import requests
from typing import Dict, Any, List
from google import genai
from google.genai import types
from api.weather import get_weather

class AIOptimizer:
    def __init__(self, gemini_api_key: str, openweather_api_key: str = None):
        # We no longer strictly need openweather_api_key, but keeping it in signature if main.py passes it
        self.openweather_api_key = openweather_api_key 
        if gemini_api_key:
            self.client = genai.Client(api_key=gemini_api_key)
        else:
            self.client = None

    def _get_elevation(self, lat: float, lng: float) -> float:
        """
        Fetches elevation using Open-Elevation API.
        Note: Public API can be slow. In production, use a premium service.
        """
        url = f"https://api.open-elevation.com/api/v1/lookup?locations={lat},{lng}"
        try:
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                data = response.json()
                return data["results"][0]["elevation"]
        except Exception:
            pass
        return 0.0

    def analyze_segment_efficiency(self, start_lat: float, start_lng: float, end_lat: float, end_lng: float, car_range_km: float) -> Dict[str, Any]:
        """
        Uses LLM to reason about range based on weather and elevation.
        """
        weather = get_weather(end_lat, end_lng)
        start_elev = self._get_elevation(start_lat, start_lng)
        end_elev = self._get_elevation(end_lat, end_lng)
        elevation_change = end_elev - start_elev

        prompt = f"""
        You are an AI EV Range Optimizer.
        Analyze the following route segment and provide an efficiency penalty or bonus.
        
        Data:
        - Car Base Range: {car_range_km} km
        - Weather at destination: {weather['description']}, Temperature: {weather['temp']} °C, Wind Speed: {weather['wind_speed']} km/h
        - Elevation Change: {elevation_change} meters (Start: {start_elev}m, End: {end_elev}m)
        
        Reason about how the wind, temperature, and elevation change will affect the EV's range. 
        Provide a detailed explanation and an estimated percentage to reduce (or increase) the efficiency by.
        
        Respond ONLY with a JSON object in this exact format, with no markdown formatting:
        {{
            "reasoning": "Since there is a 15km/h headwind and a 200m climb ahead, reduce efficiency by 12%.",
            "efficiency_modifier_percent": -12.0
        }}
        """

        if self.client:
            try:
                response = self.client.models.generate_content(
                    model='gemini-2.5-flash-lite',
                    contents=prompt
                )
                # Clean up potential markdown formatting in the response
                result_text = response.text.strip()
                if result_text.startswith("```json"):
                    result_text = result_text[7:-3]
                elif result_text.startswith("```"):
                    result_text = result_text[3:-3]
                
                result = json.loads(result_text)
                return {
                    "reasoning": result.get("reasoning", "Standard efficiency applied."),
                    "modifier": result.get("efficiency_modifier_percent", 0.0),
                    "weather": weather,
                    "elevation_change": elevation_change
                }
            except Exception as e:
                print(f"LLM Generation failed: {e}")
                
        # Fallback if no LLM or failed
        modifier = 0.0
        if weather["temp"] < 10:
            modifier -= 10
        if elevation_change > 100:
            modifier -= 5
            
        return {
            "reasoning": f"Fallback logic: Temp {weather['temp']}°C, Elev Change {elevation_change}m.",
            "modifier": modifier,
            "weather": weather,
            "elevation_change": elevation_change
        }
        
    def filter_high_confidence_chargers(self, chargers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Only suggests a stop if the Confidence Score of a charger (based on recent check-ins and power output) is >85%.
        Synthesized Pulse Energy Network chargers receive an automatic trust boost.
        """
        high_confidence = []
        for charger in chargers:
            score = 50 # Base score
            
            # Synthesized charger trust boost (IDs starting with PULSE_ are our verified network)
            charger_id = str(charger.get("ID", ""))
            if charger_id.startswith("PULSE_"):
                score += 30  # Trusted synthesized infrastructure
            
            # Power output score
            power = charger.get("max_ccs2_power", 0)
            if power >= 150:
                score += 20
            elif power >= 50:
                score += 10
                
            # Recent check-ins score
            comments = charger.get("UserComments", [])
            has_recent_positive = False
            if comments is not None:
                for comment in comments:
                    status_id = comment.get("CheckinStatusType", {}).get("ID")
                    if status_id in (10, 140): # Success
                        score += 25
                        has_recent_positive = True
                        break
                        
            if has_recent_positive:
                score += 10
                
            # Operator Reliability Bonus
            operator = charger.get("OperatorInfo", {}).get("Title", "")
            if operator in ["Ionity", "Fastned", "Tesla", "Pulse Energy", "Tata Power", "Jio-bp"]:
                score += 10
                
            # Cap at 100
            score = min(score, 100)
            charger["ai_confidence_score"] = score
            
            if score > 85:
                high_confidence.append(charger)
                
        return high_confidence
