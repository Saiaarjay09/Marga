"""Shared geometry helpers used by the simulator and weather sampling."""

from __future__ import annotations

import math

EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (math.sin(d_lat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2)
    return 2 * EARTH_RADIUS_KM * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial compass bearing (0-360, 0=N) travelling from point 1 to point 2."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_lambda = math.radians(lon2 - lon1)
    x = math.sin(d_lambda) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(d_lambda)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def nearest_route_point(route_lat, route_lng, lat: float, lng: float):
    """Index and distance (km) of the route point closest to (lat, lng). Route arrays are numpy arrays."""
    import numpy as np

    d_lat = np.radians(route_lat - lat)
    d_lng = np.radians(route_lng - lng)
    a = np.sin(d_lat / 2) ** 2 + np.cos(np.radians(lat)) * np.cos(np.radians(route_lat)) * np.sin(d_lng / 2) ** 2
    dist = 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(a))
    idx = int(np.argmin(dist))
    return idx, float(dist[idx])
