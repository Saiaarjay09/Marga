"""
Trip simulator: walks the route in energy space (Wh) instead of a flat
km-range approximation, using core.physics for per-segment consumption
(drag, rolling resistance, grade, headwind, HVAC load, driver style) and
core.charger_registry's health overlay to avoid currently-down stations.

For every charging stop it also nominates a backup station nearby, so if
the primary turns out to be broken/occupied on arrival there's a fallback
already identified rather than the trip just failing.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from core.geo import bearing_deg, haversine_km
from core.physics import SegmentConditions, segment_energy_wh
from data.vehicles import VehicleSpec

DEFAULT_WEATHER = {"temperature": 28.0, "wind_speed_kmh": 12.0, "wind_deg": 0.0}
BACKUP_SEARCH_WINDOW_KM = 15.0  # how far along-route we'll look for an alternate to the chosen stop


def _nearest_weather(weather_samples: Optional[List[Dict[str, Any]]], point_index: int) -> Dict[str, Any]:
    if not weather_samples:
        return DEFAULT_WEATHER
    best = min(weather_samples, key=lambda s: abs(s["index"] - point_index))
    return best.get("weather", DEFAULT_WEATHER)


def _headwind_kmh(wind_speed_kmh: float, wind_from_deg: float, travel_bearing_deg: float) -> float:
    import math
    angle = math.radians(wind_from_deg - travel_bearing_deg)
    return wind_speed_kmh * math.cos(angle)


def _build_energy_profile(route_geometry: Sequence[Sequence[float]], vehicle: VehicleSpec,
                           avg_speed_kmh: float, driver_style: str,
                           elevation_profile: Optional[List[float]],
                           weather_samples: Optional[List[Dict[str, Any]]]):
    n = len(route_geometry)
    physical_km = [0.0] * n
    energy_wh = [0.0] * n

    for i in range(1, n):
        lat1, lon1 = route_geometry[i - 1][1], route_geometry[i - 1][0]
        lat2, lon2 = route_geometry[i][1], route_geometry[i][0]
        dist_km = haversine_km(lat1, lon1, lat2, lon2)
        physical_km[i] = physical_km[i - 1] + dist_km

        elev1 = elevation_profile[i - 1] if elevation_profile else 0.0
        elev2 = elevation_profile[i] if elevation_profile else 0.0
        weather = _nearest_weather(weather_samples, i)
        bearing = bearing_deg(lat1, lon1, lat2, lon2)
        headwind = _headwind_kmh(
            weather.get("wind_speed_kmh", 12.0), weather.get("wind_deg", 0.0), bearing
        )

        cond = SegmentConditions(
            distance_km=dist_km,
            avg_speed_kmh=avg_speed_kmh,
            elevation_gain_m=elev2 - elev1,
            altitude_m=(elev1 + elev2) / 2.0,
            headwind_kmh=headwind,
            ambient_temp_c=weather.get("temperature", 28.0),
        )
        seg_wh = segment_energy_wh(vehicle, cond, driver_style)
        energy_wh[i] = energy_wh[i - 1] + seg_wh

    return physical_km, energy_wh


def _project_chargers(chargers: List[Dict[str, Any]], route_geometry: Sequence[Sequence[float]],
                       physical_km: List[float], energy_wh: List[float],
                       vehicle: VehicleSpec) -> List[Dict[str, Any]]:
    projections = []
    for c in chargers:
        addr = c.get("AddressInfo", {})
        c_lat, c_lng = addr.get("Latitude"), addr.get("Longitude")
        if c_lat is None or c_lng is None:
            continue

        min_d, best_idx = float("inf"), 0
        for i, pt in enumerate(route_geometry):
            d = haversine_km(pt[1], pt[0], c_lat, c_lng)
            if d < min_d:
                min_d, best_idx = d, i

        detour_wh = min_d * 2.0 * vehicle.efficiency_wh_km  # there-and-back, flat-efficiency approximation

        projections.append({
            "charger": c,
            "route_km": physical_km[best_idx],
            "route_energy_wh": energy_wh[best_idx],
            "dist_off_route_km": min_d,
            "detour_wh": detour_wh,
            "power_kw": c.get("max_ccs2_power", 0),
            "is_working": c.get("is_working", True),
        })
    return projections


def simulate_trip(route_geometry: Sequence[Sequence[float]], vehicle: VehicleSpec,
                   safety_buffer_pct: float, reliability_toggle: bool,
                   driver_style: str = "Normal",
                   pre_fetched_chargers: Optional[List[Dict[str, Any]]] = None,
                   avg_speed_kmh: float = 60.0,
                   elevation_profile: Optional[List[float]] = None,
                   weather_samples: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    if not route_geometry:
        return {"status": "error", "message": "Empty route", "stops": []}
    if not pre_fetched_chargers:
        return {"status": "failed", "message": "No chargers available on this route.", "stops": []}

    physical_km, energy_wh = _build_energy_profile(
        route_geometry, vehicle, avg_speed_kmh, driver_style, elevation_profile, weather_samples
    )
    total_physical_km = physical_km[-1]
    total_energy_wh = energy_wh[-1]

    battery_wh_full = vehicle.battery_kwh * 1000.0
    soft_threshold_wh = battery_wh_full * (safety_buffer_pct / 100.0)
    hard_floor_wh = soft_threshold_wh * 0.5

    projections = _project_chargers(pre_fetched_chargers, route_geometry, physical_km, energy_wh, vehicle)

    current_soc_wh = battery_wh_full
    current_position_wh = 0.0
    current_position_km = 0.0
    stops: List[Dict[str, Any]] = []

    MAX_ITERATIONS = 100
    iterations = 0

    while current_position_wh + current_soc_wh - soft_threshold_wh < total_energy_wh:
        iterations += 1
        if iterations > MAX_ITERATIONS:
            return {"status": "failed", "message": "Trip failed: routing loop did not converge.", "stops": stops}

        candidates = []
        for cp in projections:
            if cp["route_energy_wh"] <= current_position_wh + 1.0:
                continue  # not strictly ahead of us

            required_wh = (cp["route_energy_wh"] - current_position_wh) + cp["detour_wh"]
            remaining_wh = current_soc_wh - required_wh
            is_dc_fast = cp["power_kw"] > 50
            healthy = cp["is_working"]

            if reliability_toggle and not healthy:
                tier = "unhealthy"  # kept only as an absolute last resort
            elif remaining_wh >= soft_threshold_wh:
                tier = "standard"
            elif hard_floor_wh <= remaining_wh < soft_threshold_wh and is_dc_fast:
                tier = "flex"
            else:
                continue

            candidates.append({
                "cp": cp, "required_wh": required_wh, "remaining_wh": remaining_wh,
                "is_dc_fast": is_dc_fast, "tier": tier,
            })

        pool = [c for c in candidates if c["tier"] in ("standard", "flex")]
        if not pool:
            pool = [c for c in candidates if c["tier"] == "unhealthy"]  # stranded fallback only

        if not pool:
            return {
                "status": "failed",
                "message": f"Trip failed: stranded with no reachable chargers ahead. "
                           f"Reached {current_position_km:.1f} km.",
                "stops": stops,
            }

        max_route_km = max(c["cp"]["route_km"] for c in pool)
        zone_start = max_route_km - 30.0
        final_zone = [c for c in pool if c["cp"]["route_km"] >= zone_start] or pool

        def score(c):
            power_bonus = c["cp"]["power_kw"] * 10
            health_bonus = 15 if c["cp"]["is_working"] else -50
            return power_bonus + health_bonus + c["cp"]["route_km"]

        final_zone.sort(key=score, reverse=True)
        best = final_zone[0]

        pool_tiers = {c["tier"] for c in pool}
        backup_candidates = [
            c for c in candidates
            if c["cp"]["charger"].get("ID") != best["cp"]["charger"].get("ID") and c["tier"] in pool_tiers
        ]
        backup = None
        if backup_candidates:
            backup = min(backup_candidates, key=lambda c: abs(c["cp"]["route_km"] - best["cp"]["route_km"]))
            if abs(backup["cp"]["route_km"] - best["cp"]["route_km"]) > BACKUP_SEARCH_WINDOW_KM * 4:
                backup = None  # nothing usefully close enough to call a real backup

        cp = best["cp"]
        if best["tier"] == "unhealthy":
            note = "⚠️ Last resort: every reachable station is currently flagged as possibly down."
        elif best["tier"] == "flex":
            note = f"Flex stop: dipped into the safety buffer (arrival buffer {best['remaining_wh'] / 1000:.1f} kWh) to reach a >50kW DC fast charger."
        else:
            note = f"Look-ahead stop: arrives with {best['remaining_wh'] / 1000:.1f} kWh to spare."

        stop_info = {
            "charger": cp["charger"],
            "backup_charger": backup["cp"]["charger"] if backup else None,
            "distance_from_route_km": cp["dist_off_route_km"],
            "power_kw": cp["power_kw"],
            "stopped_at_km": cp["route_km"],
            "note": note,
            "is_working": cp["is_working"],
        }
        stops.append(stop_info)

        current_position_wh = cp["route_energy_wh"]
        current_position_km = cp["route_km"]
        current_soc_wh = battery_wh_full * 0.85 - cp["detour_wh"]

    return {
        "status": "success",
        "message": "Trip completed successfully.",
        "total_distance": total_physical_km,
        "total_energy_wh": total_energy_wh,
        "avg_wh_per_km": (total_energy_wh / total_physical_km) if total_physical_km else 0.0,
        "stops": stops,
    }


class Simulator:
    """Thin OO wrapper kept for backwards compatibility with existing call sites."""

    def __init__(self, charger_client=None):
        self.client = charger_client

    def simulate_trip(self, route_geometry, vehicle: VehicleSpec, safety_buffer_pct: float,
                       reliability_toggle: bool, driver_style: str = "Normal", **kwargs):
        return simulate_trip(route_geometry, vehicle, safety_buffer_pct, reliability_toggle,
                              driver_style=driver_style, **kwargs)
