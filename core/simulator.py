"""
Trip simulator: walks the route in energy space (Wh) using core.physics for
per-segment consumption (drag, wind, rolling resistance, grade, climate load,
driving style) and the charger health overlay to avoid stations currently
flagged down. Every stop also gets a nearby backup station.

Returns the stops plus everything the UI needs to draw the battery graph and
the "where did the energy go" breakdown.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from core.geo import bearing_deg, haversine_km, nearest_route_point
from core.physics import SegmentConditions, segment_energy_parts
from data.vehicles import VehicleSpec

DEFAULT_WEATHER = {"temperature": 28.0, "wind_speed_kmh": 12.0, "wind_deg": 0.0}
BACKUP_MAX_GAP_KM = 60.0   # a backup further than this from the primary isn't a useful backup
CHARGE_TARGET = 0.85       # DC fast charging tapers hard above ~85%
PLUG_OVERHEAD_MIN = 5.0    # parking, plugging in, authorising
EFFECTIVE_POWER_FACTOR = 0.72  # average delivered power vs the charger's rated peak, over a 10-85% session
VEHICLE_ACCEPT_CAP_KW = 150.0  # most cars in the database can't take more than this on average


def _nearest_weather(weather_samples: Optional[List[Dict[str, Any]]], point_index: int) -> Dict[str, Any]:
    if not weather_samples:
        return DEFAULT_WEATHER
    best = min(weather_samples, key=lambda s: abs(s["index"] - point_index))
    return best.get("weather", DEFAULT_WEATHER)


def _headwind_kmh(wind_speed_kmh: float, wind_from_deg: float, travel_bearing_deg: float) -> float:
    return wind_speed_kmh * math.cos(math.radians(wind_from_deg - travel_bearing_deg))


def _build_energy_profile(route_geometry, vehicle: VehicleSpec, avg_speed_kmh: float, driver_style: str,
                           elevation_profile, weather_samples):
    n = len(route_geometry)
    physical_km = [0.0] * n
    energy_wh = [0.0] * n
    totals = {"aero": 0.0, "wind": 0.0, "rolling": 0.0, "hills": 0.0, "climate": 0.0, "style": 0.0}

    for i in range(1, n):
        lat1, lon1 = route_geometry[i - 1][1], route_geometry[i - 1][0]
        lat2, lon2 = route_geometry[i][1], route_geometry[i][0]
        dist_km = haversine_km(lat1, lon1, lat2, lon2)
        physical_km[i] = physical_km[i - 1] + dist_km

        elev1 = elevation_profile[i - 1] if elevation_profile else 0.0
        elev2 = elevation_profile[i] if elevation_profile else 0.0
        weather = _nearest_weather(weather_samples, i)
        headwind = _headwind_kmh(
            weather.get("wind_speed_kmh", 12.0), weather.get("wind_deg", 0.0),
            bearing_deg(lat1, lon1, lat2, lon2),
        )
        cond = SegmentConditions(
            distance_km=dist_km, avg_speed_kmh=avg_speed_kmh, elevation_gain_m=elev2 - elev1,
            altitude_m=(elev1 + elev2) / 2.0, headwind_kmh=headwind,
            ambient_temp_c=weather.get("temperature", 28.0),
        )
        parts = segment_energy_parts(vehicle, cond, driver_style)
        for key, value in parts.items():
            totals[key] += value
        energy_wh[i] = energy_wh[i - 1] + sum(parts.values())

    return physical_km, energy_wh, totals


def _project_chargers(chargers, route_geometry, physical_km, energy_wh, vehicle: VehicleSpec):
    route_lat = np.array([p[1] for p in route_geometry])
    route_lng = np.array([p[0] for p in route_geometry])
    projections = []
    for c in chargers:
        addr = c.get("AddressInfo", {})
        c_lat, c_lng = addr.get("Latitude"), addr.get("Longitude")
        if c_lat is None or c_lng is None:
            continue
        idx, off_km = nearest_route_point(route_lat, route_lng, c_lat, c_lng)
        projections.append({
            "charger": c,
            "route_idx": idx,
            "route_km": physical_km[idx],
            "route_energy_wh": energy_wh[idx],
            "dist_off_route_km": off_km,
            "detour_wh": off_km * 2.0 * vehicle.efficiency_wh_km,  # there and back, flat-efficiency estimate
            "power_kw": c.get("max_ccs2_power", 0),
            "is_working": c.get("is_working", True),
        })
    return projections


def _charge_minutes(energy_wh: float, power_kw: float) -> float:
    effective_kw = max(10.0, min(power_kw, VEHICLE_ACCEPT_CAP_KW) * EFFECTIVE_POWER_FACTOR)
    return PLUG_OVERHEAD_MIN + (energy_wh / 1000.0) / effective_kw * 60.0


def _finalize_stops(stops: List[Dict[str, Any]]) -> None:
    for stop in stops:
        stop["charge_minutes"] = _charge_minutes(max(stop["depart_wh"] - stop["arrival_wh"], 0.0), stop["power_kw"])


def simulate_trip(route_geometry: Sequence[Sequence[float]], vehicle: VehicleSpec,
                   safety_buffer_pct: float, reliability_toggle: bool,
                   driver_style: str = "Normal",
                   pre_fetched_chargers: Optional[List[Dict[str, Any]]] = None,
                   avg_speed_kmh: float = 60.0,
                   elevation_profile: Optional[List[float]] = None,
                   weather_samples: Optional[List[Dict[str, Any]]] = None,
                   start_soc_pct: float = 100.0) -> Dict[str, Any]:
    if not route_geometry:
        return {"status": "error", "message": "Empty route", "stops": []}

    physical_km, energy_wh, factor_totals = _build_energy_profile(
        route_geometry, vehicle, avg_speed_kmh, driver_style, elevation_profile, weather_samples
    )
    total_km = physical_km[-1]
    total_energy_wh = energy_wh[-1]

    battery_wh = vehicle.battery_kwh * 1000.0
    soft_wh = battery_wh * (safety_buffer_pct / 100.0)
    hard_wh = soft_wh * 0.5
    start_soc_wh = battery_wh * max(5.0, min(100.0, start_soc_pct)) / 100.0

    projections = _project_chargers(pre_fetched_chargers or [], route_geometry, physical_km, energy_wh, vehicle)

    soc_wh = start_soc_wh
    pos_wh = 0.0
    pos_km = 0.0
    stops: List[Dict[str, Any]] = []
    legs = [{"start_idx": 0, "start_wh": 0.0, "start_soc": soc_wh}]

    result_base = {
        "total_distance": total_km,
        "total_energy_wh": total_energy_wh,
        "avg_wh_per_km": (total_energy_wh / total_km) if total_km else 0.0,
        "factor_wh": factor_totals,
        "energy_wh_by_point": energy_wh,
        "km_by_point": physical_km,
    }

    for _ in range(100):
        if pos_wh + soc_wh - soft_wh >= total_energy_wh:
            break

        candidates = []
        for cp in projections:
            if cp["route_energy_wh"] <= pos_wh + 1.0:
                continue
            required_wh = (cp["route_energy_wh"] - pos_wh) + cp["detour_wh"] / 2.0
            remaining_wh = soc_wh - required_wh
            is_dc_fast = cp["power_kw"] > 50

            if reliability_toggle and not cp["is_working"] and remaining_wh >= hard_wh:
                tier = "unhealthy"
            elif remaining_wh >= soft_wh:
                tier = "standard"
            elif hard_wh <= remaining_wh < soft_wh and is_dc_fast:
                tier = "flex"
            else:
                continue
            candidates.append({"cp": cp, "remaining_wh": remaining_wh, "tier": tier})

        pool = [c for c in candidates if c["tier"] in ("standard", "flex")]
        if not pool:
            pool = [c for c in candidates if c["tier"] == "unhealthy"]
        if not pool:
            _finalize_stops(stops)
            return {**result_base, "status": "failed", "stops": stops, "legs": legs,
                    "message": f"No reachable charger ahead after {pos_km:.0f} km. Try a lower safety buffer, "
                               f"a fuller start charge, or a different route."}

        far_km = max(c["cp"]["route_km"] for c in pool)
        zone = [c for c in pool if c["cp"]["route_km"] >= far_km - 30.0] or pool

        def score(c):
            return c["cp"]["power_kw"] * 10 + (15 if c["cp"]["is_working"] else -50) + c["cp"]["route_km"]

        best = max(zone, key=score)
        best_id = best["cp"]["charger"].get("ID")
        pool_tiers = {c["tier"] for c in pool}
        backup_options = [
            c for c in candidates
            if c["cp"]["charger"].get("ID") != best_id
            and c["tier"] in pool_tiers
            and (c["cp"]["is_working"] or not best["cp"]["is_working"])
        ]
        backup = None
        if backup_options:
            backup = min(backup_options, key=lambda c: abs(c["cp"]["route_km"] - best["cp"]["route_km"]))
            if abs(backup["cp"]["route_km"] - best["cp"]["route_km"]) > BACKUP_MAX_GAP_KM:
                backup = None

        cp = best["cp"]
        arrival_wh = soc_wh - (cp["route_energy_wh"] - pos_wh) - cp["detour_wh"] / 2.0
        depart_wh = battery_wh * CHARGE_TARGET - cp["detour_wh"] / 2.0
        if best["tier"] == "unhealthy":
            note = "Every reachable station nearby is flagged as possibly down; this is the best of them."
        elif best["tier"] == "flex":
            note = "Tight but safe: dips into your buffer to reach a fast charger."
        else:
            note = ""

        stops.append({
            "charger": cp["charger"],
            "backup_charger": backup["cp"]["charger"] if backup else None,
            "backup_gap_km": abs(backup["cp"]["route_km"] - cp["route_km"]) if backup else None,
            "detour_km": cp["dist_off_route_km"],
            "power_kw": cp["power_kw"],
            "stopped_at_km": cp["route_km"],
            "route_idx": cp["route_idx"],
            "arrival_wh": arrival_wh,
            "depart_wh": depart_wh,
            "note": note,
            "is_working": cp["is_working"],
        })
        legs.append({"start_idx": cp["route_idx"], "start_wh": cp["route_energy_wh"], "start_soc": depart_wh})

        pos_wh, pos_km, soc_wh = cp["route_energy_wh"], cp["route_km"], depart_wh
    else:
        _finalize_stops(stops)
        return {**result_base, "status": "failed", "stops": stops, "legs": legs,
                "message": "Route planning did not converge."}

    # Only charge as much as the final leg actually needs, plus the buffer.
    if stops:
        last = stops[-1]
        detour_half = last["detour_km"] * vehicle.efficiency_wh_km
        remaining = total_energy_wh - energy_wh[last["route_idx"]]
        needed = remaining + soft_wh + battery_wh * 0.03 + detour_half
        last["depart_wh"] = min(last["depart_wh"], max(needed, last["arrival_wh"]))
        legs[-1]["start_soc"] = last["depart_wh"]

    _finalize_stops(stops)

    final_leg = legs[-1]
    arrival_soc_wh = final_leg["start_soc"] - (total_energy_wh - final_leg["start_wh"])

    return {
        **result_base,
        "status": "success",
        "message": "Trip planned.",
        "stops": stops,
        "legs": legs,
        "start_soc_wh": start_soc_wh,
        "arrival_soc_pct": arrival_soc_wh / battery_wh * 100.0,
    }
