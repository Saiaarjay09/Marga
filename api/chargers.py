"""
Live Open Charge Map client, used to top up the local registry with any
station added since the last refresh_chargers.py run. No fabricated /
synthesized station data -- a previous version of this file invented
fictional "Pulse Energy Network" charging stations along every route
(reverse-geocoded names and all) and presented them as real, which is
actively dangerous: a driver could route to a stop that doesn't exist.
Every charger this module returns comes from Open Charge Map.
"""

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import requests


class OpenChargeMapClient:
    """Client for Open Charge Map API with reliability filters for 4-wheelers."""

    def __init__(self, api_key: str = None):
        self.api_key = api_key
        self.base_url = "https://api.openchargemap.io/v3/poi"
        # 2-wheeler / unsupported networks that don't serve CCS2 4-wheeler charging
        self.network_blacklist = ["ather", "ola", "revolt", "bounce", "yulu", "e:swap", "tvs", "honda"]

    def _fetch_chargers(self, lat: float, lng: float, distance_km: float, verbose: bool = False) -> List[Dict[str, Any]]:
        params = {
            "latitude": round(float(lat), 4),
            "longitude": round(float(lng), 4),
            "distance": distance_km,
            "distanceunit": "KM",
            "statustypeid": 50,          # Operational
            "connectiontypeid": 33,      # CCS2
            "minpowerkw": 25,
            "maxresults": 100,
            "compact": not verbose,
            "verbose": verbose,
        }
        if self.api_key:
            params["key"] = self.api_key

        headers = {"User-Agent": "EV-Copilot-Planner/1.0"}

        try:
            response = requests.get(self.base_url, params=params, headers=headers, timeout=10)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"OCM API fetch error: {e}")
            return []

    def _calculate_distance(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        R = 6371.0
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (math.sin(dlat / 2) ** 2
             + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
        return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    def get_chargers_along_route(self, route_geometry, corridor_km: float = 10,
                                  require_recent_checkin: bool = False, days: int = 7) -> List[Dict[str, Any]]:
        """Samples the route every ~15km and queries Open Charge Map live, deduped by ID."""
        if not route_geometry:
            return []

        all_chargers: Dict[Any, Dict[str, Any]] = {}
        sampled_points = [route_geometry[0]]
        last_point = route_geometry[0]

        for point in route_geometry[1:]:
            if self._calculate_distance(last_point[1], last_point[0], point[1], point[0]) >= 15.0:
                sampled_points.append(point)
                last_point = point
        if route_geometry[-1] not in sampled_points:
            sampled_points.append(route_geometry[-1])

        for pt in sampled_points:
            lng, lat = pt[0], pt[1]
            chargers = self.get_reliable_chargers(lat, lng, distance_km=corridor_km,
                                                   require_recent_checkin=require_recent_checkin, days=days)
            if not chargers and corridor_km < 30.0:
                chargers = self.get_reliable_chargers(lat, lng, distance_km=30.0,
                                                       require_recent_checkin=require_recent_checkin, days=days)
            for c in chargers:
                c_id = c.get("ID")
                if c_id not in all_chargers:
                    all_chargers[c_id] = c

        return list(all_chargers.values())

    def get_reliable_chargers(self, lat: float, lng: float, distance_km: float = 10,
                               require_recent_checkin: bool = False, days: int = 7) -> List[Dict[str, Any]]:
        raw_chargers = self._fetch_chargers(lat, lng, distance_km, verbose=require_recent_checkin)

        verified_chargers = []
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)

        for charger in raw_chargers:
            operator_title = charger.get("OperatorInfo", {}).get("Title", "").lower()
            if any(blacklisted in operator_title for blacklisted in self.network_blacklist):
                continue

            if charger.get("SubmissionStatus", {}).get("ID") != 100:
                continue

            has_fast_ccs2, max_power = False, 0
            for conn in charger.get("Connections", []):
                if conn.get("ConnectionTypeID") == 33:
                    power = conn.get("PowerKW", 0) or 0
                    if power >= 25:
                        has_fast_ccs2 = True
                        max_power = max(max_power, power)
            if not has_fast_ccs2:
                continue
            charger["max_ccs2_power"] = max_power

            if require_recent_checkin:
                has_recent_positive = False
                for comment in (charger.get("UserComments") or []):
                    date_str = comment.get("DateCreated")
                    if not date_str:
                        continue
                    try:
                        created_at = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    checkin_status = comment.get("CheckinStatusType", {}).get("ID")
                    if created_at >= cutoff_date and checkin_status in (10, 140):
                        has_recent_positive = True
                        break
                if not has_recent_positive:
                    continue

            verified_chargers.append(charger)

        return verified_chargers
