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

    def simulate_trip(self, route_geometry, adjusted_range, safety_buffer, reliability_toggle, pre_fetched_chargers=None, avg_speed_kmh=60.0):
        """
        Greedy look-ahead algorithm with Soft Threshold Flex Buffer and Dynamic Route Physics.
        1. Dynamic Range Check with 10% Soft Threshold and 5% Hard Floor.
        2. Flex Evaluation: Reach beyond soft threshold for DC fast chargers if none exist within soft limit.
        3. Break immediately if destination is reachable with 10% buffer.
        """
        if not route_geometry:
            return {"status": "error", "message": "Empty route"}

        stops = []
        
        # Define the thresholds based on vehicle's adjusted base range
        hard_floor_km = adjusted_range * 0.05
        soft_threshold_km = adjusted_range * 0.10
        
        # Calculate cumulative physical and effective (SoC drain) distances
        physical_route_dists = [0.0]
        effective_route_dists = [0.0]
        
        for i in range(1, len(route_geometry)):
            pos1 = route_geometry[i-1]
            pos2 = route_geometry[i]
            physical_dist = haversine_distance(pos1[0], pos1[1], pos2[0], pos2[1])
            
            # 1. Dynamic Speed Multiplier
            speed_multiplier = 1.15 if avg_speed_kmh > 95 else 1.0
            
            # 2. Geospatial Elevation Delta
            elevation_multiplier = 1.0
            if len(pos1) >= 3 and len(pos2) >= 3:
                ele_gain = pos2[2] - pos1[2]
                # +1.5% Wh/km for every 100 meters
                elevation_multiplier += (ele_gain / 100.0) * 0.015
                # Floor it at 0.5 to prevent infinite regeneration during steep descents
                elevation_multiplier = max(0.5, elevation_multiplier)
                
            segment_efficiency_multiplier = speed_multiplier * elevation_multiplier
            effective_dist = physical_dist * segment_efficiency_multiplier
            
            physical_route_dists.append(physical_route_dists[-1] + physical_dist)
            effective_route_dists.append(effective_route_dists[-1] + effective_dist)
        
        total_physical_dist = physical_route_dists[-1]
        total_effective_dist = effective_route_dists[-1]
        
        current_soc_range = adjusted_range
        current_effective_route_km = 0.0
        current_physical_route_km = 0.0
        
        charger_projections = []
        if pre_fetched_chargers:
            for c in pre_fetched_chargers:
                c_lat = c["AddressInfo"]["Latitude"]
                c_lng = c["AddressInfo"]["Longitude"]
                
                min_d = float('inf')
                best_idx = 0
                
                # Project charger onto the closest point of the polyline
                for i, pt in enumerate(route_geometry):
                    d = haversine_distance(pt[0], pt[1], c_lat, c_lng)
                    if d < min_d:
                        min_d = d
                        best_idx = i
                        
                best_physical_km = physical_route_dists[best_idx]
                best_effective_km = effective_route_dists[best_idx]
                
                charger_projections.append({
                    "charger": c,
                    "route_km": best_physical_km,
                    "effective_route_km": best_effective_km,
                    "dist_off_route": min_d,
                    "lat": c_lat,
                    "lng": c_lng,
                    "power_kw": c.get("max_ccs2_power", 0)
                })

        # Safeguard to prevent infinite loop
        MAX_ITERATIONS = 100
        iterations = 0

        # Loop breaks immediately if we can reach the destination maintaining the 10% Soft Threshold
        while current_effective_route_km + current_soc_range - soft_threshold_km < total_effective_dist:
            iterations += 1
            if iterations > MAX_ITERATIONS:
                return {
                    "status": "failed", 
                    "message": "Trip failed: Infinite loop detected in routing.",
                    "stops": stops
                }
                
            standard_chargers = []
            flex_chargers = []
            
            if charger_projections:
                for cp in charger_projections:
                    # Must be strictly ahead of us to ensure forward progress
                    if cp["effective_route_km"] > current_effective_route_km + 1.0:
                        dist_along_route_eff = cp["effective_route_km"] - current_effective_route_km
                        
                        # Detour costs apply the speed penalty too for consistency (ignoring elevation for off-route delta)
                        eff_dist_off_route = cp["dist_off_route"] * (1.15 if avg_speed_kmh > 95 else 1.0)
                        required_range = dist_along_route_eff + eff_dist_off_route
                        remaining_range = current_soc_range - required_range
                        
                        is_dc_fast = cp["power_kw"] > 50
                        
                        if remaining_range >= soft_threshold_km:
                            standard_chargers.append({
                                "cp": cp,
                                "required_range": required_range,
                                "remaining_range_at_arrival": remaining_range,
                                "is_dc_fast": is_dc_fast
                            })
                        elif hard_floor_km <= remaining_range < soft_threshold_km and is_dc_fast:
                            flex_chargers.append({
                                "cp": cp,
                                "required_range": required_range,
                                "remaining_range_at_arrival": remaining_range,
                                "is_dc_fast": is_dc_fast
                            })
            else:
                return {
                    "status": "failed",
                    "message": "Pre-fetched chargers array is empty or not provided. Look-ahead requires route chargers.",
                    "stops": stops
                }
            
            has_dc_fast_in_standard = any(c["is_dc_fast"] for c in standard_chargers)
            
            # Flex Window Evaluation
            if not has_dc_fast_in_standard and flex_chargers:
                pool = standard_chargers + flex_chargers
            else:
                pool = standard_chargers
                
            if not pool:
                return {
                    "status": "failed", 
                    "message": f"Trip failed: Stranded! No reachable chargers found ahead before 5% hard floor limit. Reached {current_physical_route_km:.1f} physical km.",
                    "stops": stops
                }
                
            # Fast-Charger Prioritization
            # Cluster in the final 30km (physical) of the evaluated pool
            max_r_km = max(rc["cp"]["route_km"] for rc in pool)
            zone_start = max_r_km - 30.0 
            
            final_zone_chargers = [rc for rc in pool if rc["cp"]["route_km"] >= zone_start]
            if not final_zone_chargers:
                final_zone_chargers = pool
                
            best_rc = None
            best_score = -999999
            for rc in final_zone_chargers:
                power = rc["cp"]["power_kw"]
                # Score heavily values power, then distance
                score = (power * 10) + rc["cp"]["route_km"]
                if score > best_score:
                    best_score = score
                    best_rc = rc
                    
            # Allocate optimal look-ahead stop
            cp = best_rc["cp"]
            
            if best_rc in flex_chargers:
                note = f"Flex Stop: Dipped into soft threshold (buffer {best_rc['remaining_range_at_arrival']:.1f} km) to reach >50kW DC Fast station. Charging to 85% SoC."
            else:
                note = f"Look-Ahead Stop: Arrived cleanly with {best_rc['remaining_range_at_arrival']:.1f} km buffer. Charging to 85% SoC."
                
            stop_info = {
                "charger": cp["charger"],
                "distance_from_route": cp["dist_off_route"],
                "power_kw": cp["power_kw"],
                "stopped_at_km": cp["route_km"],
                "search_radius_used": 0.0,
                "note": note
            }
            stops.append(stop_info)
            
            # Battery Reset
            current_effective_route_km = cp["effective_route_km"]
            current_physical_route_km = cp["route_km"]
            
            # Reset SoC to 85% due to DC fast charging curve slow-down
            current_soc_range = adjusted_range * 0.85
            # Deduct the cost of getting back on the route from the detour (effective)
            eff_dist_off_route = cp["dist_off_route"] * (1.15 if avg_speed_kmh > 95 else 1.0)
            current_soc_range -= eff_dist_off_route

        return {
            "status": "success",
            "message": "Trip completed successfully.",
            "total_distance": total_physical_dist,
            "stops": stops
        }
