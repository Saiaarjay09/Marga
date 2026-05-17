import math
import time
import requests
import polyline
from typing import Tuple, Optional, Dict, Any, List

def get_coords_from_city(city_name: str) -> Tuple[Optional[float], Optional[float]]:
    """
    Geocodes a city name using the ArcGIS endpoint, strictly locked to India bounds.
    No hardcoding, purely dynamic input with geographical safety rules.
    """
    if not city_name:
        return None, None
        
    # Adding sourceCountry=IND forces ArcGIS to ONLY search inside India, completely eliminating the Africa/Europe bug
    url = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?f=json&singleLine={city_name}&sourceCountry=IND&maxLocations=1"
    
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data.get('candidates') and len(data['candidates']) > 0:
            location = data['candidates'][0]['location']
            
            # y is Latitude, x is Longitude
            lat = float(location['y'])
            lon = float(location['x'])
            
            print(f"[GEOCODE MATCH] Input: {city_name} -> Resolved inside India to: Lat {lat}, Lng {lon}")
            return lat, lon
            
    except Exception as e:
        print(f"Error geocoding city {city_name}: {e}")
        
    return None, None

def _calculate_haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculates the great-circle distance between two points on the Earth's surface in km."""
    R = 6371.0  # Earth radius in kilometers
    
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    
    a = math.sin(delta_phi / 2.0)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    
    return R * c

def _generate_straight_line_fallback(start_coords: Tuple[float, float], end_coords: Tuple[float, float]) -> Dict[str, Any]:
    """Generates 20 incremental coordinates in a straight line path between start and end."""
    start_lat, start_lng = start_coords
    end_lat, end_lng = end_coords
    
    geometry = []
    num_points = 20
    
    for i in range(num_points):
        fraction = i / (num_points - 1)
        # Linear interpolation
        lat = start_lat + (end_lat - start_lat) * fraction
        lng = start_lng + (end_lng - start_lng) * fraction
        # Appending [lng, lat] for PyDeck PathLayer
        geometry.append([lng, lat])
        
    distance_km = _calculate_haversine_distance(start_lat, start_lng, end_lat, end_lng)
    
    # Estimate duration based on an average speed of 60 km/h (1 km/min)
    duration_min = distance_km * 1.0
    
    return {
        "geometry": geometry,
        "distance_km": distance_km,
        "duration_min": duration_min,
        "fallback": True
    }

def get_osrm_route(start_coords: Tuple[float, float], end_coords: Tuple[float, float]) -> List[Dict[str, Any]]:
    """
    Fetches real-time highway geometry from OSRM Free API with a multi-server failover mechanism.
    Returns a list of route dictionaries (up to 3 alternatives).
    coords should be (lat, lng).
    Each route dict contains: route_id, distance_km, duration_mins, geometry, fallback.
    """
    start_lat, start_lng = start_coords
    end_lat, end_lng = end_coords
    
    # Coordinate Order Fix: Ensure format is lon,lat;lon,lat
    coords_string = f"{start_lng},{start_lat};{end_lng},{end_lat}"
    
    endpoints = [
        # Primary: OSRM Server 1
        f"http://router.project-osrm.org/route/v1/driving/{coords_string}",
        # Secondary: Alternative Free OSRM Mirror
        f"https://routing.openstreetmap.de/routed-car/route/v1/driving/{coords_string}"
    ]
    
    params = {
        "overview": "full",
        "geometries": "polyline",
        "alternatives": "3"
    }
    
    for url in endpoints:
        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if data.get("code") == "Ok" and len(data.get("routes", [])) > 0:
                routes = []
                for idx, route in enumerate(data["routes"]):
                    encoded_polyline = route["geometry"]
                    # polyline.decode returns [(lat, lng)]
                    decoded = polyline.decode(encoded_polyline)
                    # Convert to [lng, lat] for PyDeck PathLayer
                    geometry = [[pt[1], pt[0]] for pt in decoded]
                    
                    routes.append({
                        "route_id": f"Route {idx + 1}",
                        "distance_km": round(route["distance"] / 1000.0, 1),
                        "duration_mins": round(route["duration"] / 60.0),
                        "geometry": geometry,
                        "fallback": False
                    })
                    
                return routes
        except requests.exceptions.RequestException as e:
            print(f"Routing endpoint {url} failed: {e}")
            continue
            
    # Tertiary Fallback: Geometric Route Interpolation
    print("All routing endpoints failed. Falling back to geometric route interpolation.")
    fallback = _generate_straight_line_fallback(start_coords, end_coords)
    return [{
        "route_id": "Route 1 (Fallback)",
        "distance_km": round(fallback["distance_km"], 1),
        "duration_mins": round(fallback["duration_min"]),
        "geometry": fallback["geometry"],
        "fallback": True
    }]