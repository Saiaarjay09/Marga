"""
Route elevation profile via the free, keyless Open-Meteo Elevation API.

We deliberately don't use Open-Elevation (the old code's choice for the
single start/end lookup) for the full route: it's rate-limited to small
batches and frequently times out. Open-Meteo accepts up to 100 coordinates
per request and is fast, so we sample the route every ~2km, batch-fetch
those, and linearly interpolate elevation for every point in between.
"""

from __future__ import annotations

import math
from typing import List, Sequence, Tuple

import requests

ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
MAX_POINTS_PER_REQUEST = 100
SAMPLE_INTERVAL_KM = 2.0


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (math.sin(d_lat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2)
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _fetch_elevations(coords: Sequence[Tuple[float, float]]) -> List[float]:
    """coords: list of (lat, lon). Returns elevations in meters, 0.0 on failure."""
    elevations: List[float] = []
    for i in range(0, len(coords), MAX_POINTS_PER_REQUEST):
        chunk = coords[i:i + MAX_POINTS_PER_REQUEST]
        lat_str = ",".join(f"{lat:.5f}" for lat, _ in chunk)
        lon_str = ",".join(f"{lon:.5f}" for _, lon in chunk)
        try:
            resp = requests.get(ELEVATION_URL, params={"latitude": lat_str, "longitude": lon_str}, timeout=8)
            resp.raise_for_status()
            data = resp.json()
            elevations.extend(float(e) for e in data.get("elevation", [0.0] * len(chunk)))
        except Exception:
            elevations.extend([0.0] * len(chunk))
    return elevations


def get_elevation_profile(route_geometry: Sequence[Sequence[float]]) -> List[float]:
    """
    route_geometry: list of [lng, lat] (or [lng, lat, ele]) points, as produced by the routing module.
    Returns a list of elevations in meters, one per input point.
    """
    n = len(route_geometry)
    if n == 0:
        return []
    if n == 1:
        elev = _fetch_elevations([(route_geometry[0][1], route_geometry[0][0])])
        return elev

    cumulative_km = [0.0]
    for i in range(1, n):
        prev, cur = route_geometry[i - 1], route_geometry[i]
        cumulative_km.append(cumulative_km[-1] + _haversine_km(prev[1], prev[0], cur[1], cur[0]))
    total_km = cumulative_km[-1]

    if total_km <= 0:
        elev = _fetch_elevations([(route_geometry[0][1], route_geometry[0][0])])
        return [elev[0] if elev else 0.0] * n

    num_samples = min(MAX_POINTS_PER_REQUEST, max(2, int(total_km / SAMPLE_INTERVAL_KM) + 1))
    sample_target_kms = [total_km * i / (num_samples - 1) for i in range(num_samples)]

    sample_indices = []
    cursor = 0
    for target in sample_target_kms:
        while cursor < n - 1 and cumulative_km[cursor] < target:
            cursor += 1
        sample_indices.append(cursor)

    sample_coords = [(route_geometry[idx][1], route_geometry[idx][0]) for idx in sample_indices]
    sample_elevations = _fetch_elevations(sample_coords)
    sample_kms = [cumulative_km[idx] for idx in sample_indices]

    profile: List[float] = []
    seg = 0
    for km in cumulative_km:
        while seg < len(sample_kms) - 2 and km > sample_kms[seg + 1]:
            seg += 1
        k0, k1 = sample_kms[seg], sample_kms[seg + 1]
        e0, e1 = sample_elevations[seg], sample_elevations[seg + 1]
        if k1 == k0:
            profile.append(e0)
        else:
            frac = (km - k0) / (k1 - k0)
            profile.append(e0 + (e1 - e0) * frac)

    return profile
