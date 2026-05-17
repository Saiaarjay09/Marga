import requests
import math
from typing import List, Dict, Any
from datetime import datetime, timedelta, timezone

class OpenChargeMapClient:
    """
    Client for Open Charge Map API handling reliability filters for 4-wheelers.
    """
    def __init__(self, api_key: str = None):
        self.api_key = api_key
        self.base_url = "https://api.openchargemap.io/v3/poi"
        # 2-Wheeler / Unsupported Network Blacklist
        self.network_blacklist = ["ather", "ola", "revolt", "bounce", "yulu"]
        
    def _fetch_chargers(self, lat: float, lng: float, distance_km: float, verbose: bool = False) -> List[Dict[str, Any]]:
        params = {
            "latitude": lat,
            "longitude": lng,
            "distance": distance_km,
            "distanceunit": "KM",
            "statustypeid": 50,          # Filter 1: Operational
            "connectiontypeid": 33,      # Filter 3: CCS2
            "minpowerkw": 25,            # Filter 2: > 25kW
            "maxresults": 100,
            "compact": not verbose,
            "verbose": verbose
        }
        if self.api_key:
            params["key"] = self.api_key
            
        headers = {
            "User-Agent": "EV-Copilot-Planner/1.0"
        }
            
        response = requests.get(self.base_url, params=params, headers=headers)
        response.raise_for_status()
        return response.json()

    def _calculate_distance(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Haversine formula to calculate distance in km."""
        R = 6371  # Earth radius in km
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (math.sin(dlat / 2) * math.sin(dlat / 2) +
             math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
             math.sin(dlon / 2) * math.sin(dlon / 2))
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return R * c

    def get_chargers_along_route(self, route_geometry: List[List[float]], corridor_km: float = 10, require_recent_checkin: bool = False, days: int = 7) -> List[Dict[str, Any]]:
        """
        Route-Snapping: Searches for chargers in a corridor along the entire OSRM path.
        route_geometry is a list of [lng, lat] points.
        """
        all_chargers = {}
        
        # Sample points along the route to query
        # Since OSRM geometry has many points, we sample one point every ~ (corridor_km * 1.5) km
        # to ensure overlapping coverage without excessive API calls.
        if not route_geometry:
            return []
            
        sampled_points = []
        last_point = route_geometry[0]
        sampled_points.append(last_point)
        
        for point in route_geometry[1:]:
            dist = self._calculate_distance(last_point[1], last_point[0], point[1], point[0])
            if dist >= (corridor_km * 1.5):
                sampled_points.append(point)
                last_point = point
                
        # Always include destination
        if route_geometry[-1] not in sampled_points:
            sampled_points.append(route_geometry[-1])

        for pt in sampled_points:
            lng, lat = pt
            chargers = self.get_reliable_chargers(lat, lng, distance_km=corridor_km, require_recent_checkin=require_recent_checkin, days=days)
            for c in chargers:
                c_id = c.get("ID")
                if c_id not in all_chargers:
                    all_chargers[c_id] = c
                    
        return list(all_chargers.values())

    def get_reliable_chargers(self, lat: float, lng: float, distance_km: float = 50, require_recent_checkin: bool = False, days: int = 7) -> List[Dict[str, Any]]:
        """
        Fetches chargers that strictly adhere to reliability guidelines and network blacklist.
        """
        # If we need checkins, we must request verbose data
        raw_chargers = self._fetch_chargers(lat, lng, distance_km, verbose=require_recent_checkin)
        
        verified_chargers = []
        now = datetime.now(timezone.utc)
        cutoff_date = now - timedelta(days=days)

        for charger in raw_chargers:
            # Blacklist Check
            operator_title = charger.get("OperatorInfo", {}).get("Title", "").lower()
            if any(blacklisted in operator_title for blacklisted in self.network_blacklist):
                continue
                
            # Filter 1: SubmissionStatusTypeID == 100 (Verified)
            sub_status = charger.get("SubmissionStatus", {}).get("ID")
            if sub_status != 100:
                continue
                
            # Filter 2 & 3 Verification at the connection level
            has_fast_ccs2 = False
            max_power = 0
            for conn in charger.get("Connections", []):
                if conn.get("ConnectionTypeID") == 33:
                    power = conn.get("PowerKW", 0) or 0
                    if power > 25:
                        has_fast_ccs2 = True
                        if power > max_power:
                            max_power = power
            
            if not has_fast_ccs2:
                continue
                
            charger["max_ccs2_power"] = max_power

            # Reliability Toggle Logic
            if require_recent_checkin:
                has_recent_positive = False
                comments = charger.get("UserComments", [])
                if comments is not None:
                    for comment in comments:
                        date_str = comment.get("DateCreated")
                        if not date_str:
                            continue
                        try:
                            # Parsing ISO format from API, e.g., '2023-10-25T14:30:00Z'
                            created_at = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                            # Status 10 = Charged Successfully, 140 = Equipment Works
                            checkin_status = comment.get("CheckinStatusType", {}).get("ID")
                            
                            if created_at >= cutoff_date and checkin_status in (10, 140):
                                has_recent_positive = True
                                break
                        except ValueError:
                            continue
                
                if not has_recent_positive:
                    continue # Skip this charger if no recent positive check-in

            verified_chargers.append(charger)

        return verified_chargers
