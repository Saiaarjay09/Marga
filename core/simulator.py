import math
from typing import List, Dict, Any, Tuple
from api.chargers import OpenChargeMapClient

def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculates the distance between two geographical points in km."""
    R = 6371.0 # Earth radius in km
    dLat = math.radians(lat2 - lat1)
    dLon = math.radians(lon2 - lon1)
    a = math.sin(dLat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dLon/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c

class Simulator:
    def __init__(self, charger_client: OpenChargeMapClient):
        self.client = charger_client

    def simulate_trip(self, route_geometry, adjusted_range, safety_buffer, reliability_toggle, pre_fetched_chargers=None):
        """
        Drives the route kilometer-by-kilometer, dynamically tracking SOC.
        Triggers search phase at 25% battery to mimic realistic driving behavior.
        """
        current_range = adjusted_range
        # Dynamic 25% (1/4th Battery) Trigger Bias
        search_trigger_range = adjusted_range * 0.25 
        
        total_distance = 0.0
        stops = []
        
        if not route_geometry:
            return {"status": "error", "message": "Empty route"}

        current_pos = route_geometry[0]
        
        for i in range(1, len(route_geometry)):
            next_pos = route_geometry[i]
            segment_dist = haversine_distance(current_pos[0], current_pos[1], next_pos[0], next_pos[1])
            dist_covered = 0.0
            
            while dist_covered < segment_dist:
                # Track dynamically kilometer-by-kilometer
                increment = min(1.0, segment_dist - dist_covered)
                current_range -= increment
                total_distance += increment
                dist_covered += increment
                
                # Interpolate position
                ratio = dist_covered / segment_dist
                lat = current_pos[0] + (next_pos[0] - current_pos[0]) * ratio
                lng = current_pos[1] + (next_pos[1] - current_pos[1]) * ratio
                
                # Trigger Active Search Phase at 25% battery
                if current_range <= search_trigger_range:
                    chargers = []
                    
                    # Proactive Detour and Grid Expansion Logic
                    # Search radiuses expanding up to 30km off-track
                    search_radii = [10.0, 20.0, 30.0]
                    used_radius = 10.0
                    
                    for radius in search_radii:
                        if pre_fetched_chargers:
                            current_radius_chargers = []
                            for c in pre_fetched_chargers:
                                c_lat = c["AddressInfo"]["Latitude"]
                                c_lng = c["AddressInfo"]["Longitude"]
                                if haversine_distance(lat, lng, c_lat, c_lng) <= radius:
                                    current_radius_chargers.append(c)
                            if current_radius_chargers:
                                chargers = current_radius_chargers
                                used_radius = radius
                                break
                        else:
                            fetched = self.client.get_reliable_chargers(
                                lat=lat, 
                                lng=lng, 
                                distance_km=radius, 
                                require_recent_checkin=reliability_toggle
                            )
                            if fetched:
                                chargers = fetched
                                used_radius = radius
                                break
                    
                    # Human Driver Compromise (Adaptive Buffering)
                    # If nothing is found strictly within 30km, look ahead up to 100km total area.
                    if not chargers:
                        extended_radius = 100.0
                        if pre_fetched_chargers:
                            for c in pre_fetched_chargers:
                                c_lat = c["AddressInfo"]["Latitude"]
                                c_lng = c["AddressInfo"]["Longitude"]
                                if haversine_distance(lat, lng, c_lat, c_lng) <= extended_radius:
                                    chargers.append(c)
                        else:
                            chargers = self.client.get_reliable_chargers(
                                lat=lat, 
                                lng=lng, 
                                distance_km=extended_radius, 
                                require_recent_checkin=reliability_toggle
                            )
                            
                    if not chargers:
                        return {
                            "status": "failed", 
                            "message": f"Trip failed: Stranded! No verified chargers within 100km area at distance {total_distance:.1f}km.",
                            "stops": stops
                        }
                    
                    # Score and pick the best charger
                    best_charger = None
                    best_score = -999999
                    
                    for c in chargers:
                        c_lat = c["AddressInfo"]["Latitude"]
                        c_lng = c["AddressInfo"]["Longitude"]
                        dist_to_c = haversine_distance(lat, lng, c_lat, c_lng)
                        power = c.get("max_ccs2_power", 0)
                        
                        # We allow a slight dip into the safety buffer margin (e.g. going 5km beyond strictly available range)
                        reachable_range = current_range + 5.0 
                        if dist_to_c > reachable_range:
                            continue # Physically unreachable
                            
                        is_high_speed = 1 if power >= 50 else 0
                        
                        # Prioritize high speed, then shortest detour
                        score = (is_high_speed * 1000) - dist_to_c
                        if score > best_score:
                            best_score = score
                            best_charger = (c, dist_to_c, power)
                            
                    if best_charger:
                        c_data, dist_to_c, power = best_charger
                        
                        stop_info = {
                            "charger": c_data,
                            "distance_from_route": dist_to_c,
                            "power_kw": power,
                            "stopped_at_km": total_distance,
                            "search_radius_used": used_radius
                        }
                        
                        # Adjust battery consumption for the detour
                        current_range -= dist_to_c
                        
                        if current_range < safety_buffer:
                            soc_percent = (current_range / adjusted_range) * 100
                            stop_info["note"] = f"Adaptive Buffering: Pushed through to {soc_percent:.1f}% SOC to reach superior high-speed hub."
                        elif dist_to_c > 5.0:
                            stop_info["note"] = f"Proactive Detour: Deviated {dist_to_c:.1f}km off-route to reach reliable station."
                            
                        stops.append(stop_info)
                        
                        # Recharge back to full (minus the detour distance returning to route)
                        current_range = adjusted_range - dist_to_c
                    else:
                        return {
                            "status": "failed",
                            "message": f"Failed to select a reachable charger. Range left: {current_range:.1f}km.",
                            "stops": stops
                        }
                        
            current_pos = next_pos
            
        return {
            "status": "success",
            "message": "Trip completed successfully.",
            "total_distance": total_distance,
            "stops": stops
        }
