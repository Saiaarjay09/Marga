"""
Marga web server.

One process serves both the JSON API and the static frontend (web/).
Run:  uvicorn server:asgi_app --host 127.0.0.1 --port 8090
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import threading
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

import numpy as np
import requests
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from api.routing import get_osrm_route
from api.weather import get_weather, sample_weather_along_route
from core.ai_optimizer import AIOptimizer
from core.charger_registry import HEALTH_PATH, REGISTRY_PATH, get_registry_with_health
from core.elevation import get_elevation_profile
from core.geo import nearest_route_point
from core.physics import DRIVER_STYLE_MULTIPLIERS, flat_baseline_range_km
from core.simulator import simulate_trip
from data.vehicles import VEHICLE_DB, VehicleSpec

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(ROOT, "web")
URL_PREFIX = "/marga"          # Tailscale serves the site at <host>/marga; we accept both that and "/"
CORRIDOR_KM = 10.0
MIN_CHARGER_KW = 25
TWO_WHEELER_NETWORKS = ("ather", "ola", "revolt", "bounce", "yulu", "e:swap", "tvs", "honda")
INDIA_BOUNDS = (6.0, 37.5, 68.0, 98.5)  # lat_min, lat_max, lng_min, lng_max
GEOCODE_URL = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer"
REFRESH_INTERVAL_S = 24 * 3600


# --------------------------------------------------------------------------- #
# Charger registry (in-memory, reloads when the files on disk change)
# --------------------------------------------------------------------------- #

class RegistryCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._stamp = None
        self._data: Dict[str, Any] = {"chargers": [], "registry_last_updated": None, "health_checked_count": 0}
        self.version = 0

    @staticmethod
    def _mtime(path: str) -> float:
        try:
            return os.path.getmtime(os.path.join(ROOT, path))
        except OSError:
            return 0.0

    def get(self) -> Dict[str, Any]:
        stamp = (self._mtime(REGISTRY_PATH), self._mtime(HEALTH_PATH))
        with self._lock:
            if stamp != self._stamp:
                data = get_registry_with_health(os.path.join(ROOT, REGISTRY_PATH), os.path.join(ROOT, HEALTH_PATH))
                usable = []
                for c in data["chargers"]:
                    operator = ((c.get("OperatorInfo") or {}).get("Title") or "").lower()
                    if any(n in operator for n in TWO_WHEELER_NETWORKS):
                        continue
                    conns = c.get("Connections") or []
                    power = max((x.get("PowerKW") or 0 for x in conns), default=0)
                    if power < MIN_CHARGER_KW:
                        continue
                    c["max_ccs2_power"] = power
                    usable.append(c)
                data["chargers"] = usable
                data["down_count"] = sum(1 for c in usable if not c.get("is_working", True))
                self._data, self._stamp = data, stamp
                self.version += 1
            return self._data


registry = RegistryCache()


# --------------------------------------------------------------------------- #
# Route store: routes live server-side so the client never re-uploads geometry
# --------------------------------------------------------------------------- #

class RouteStore:
    TTL_S = 3600
    MAX_ITEMS = 300

    def __init__(self):
        self._items: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def put(self, entry: Dict[str, Any]) -> str:
        route_id = uuid.uuid4().hex[:16]
        entry["created"] = time.time()
        with self._lock:
            self._evict()
            self._items[route_id] = entry
        return route_id

    def get(self, route_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            entry = self._items.get(route_id)
            if entry and time.time() - entry["created"] > self.TTL_S:
                del self._items[route_id]
                return None
            return entry

    def _evict(self):
        now = time.time()
        for k in [k for k, v in self._items.items() if now - v["created"] > self.TTL_S]:
            del self._items[k]
        while len(self._items) >= self.MAX_ITEMS:
            del self._items[min(self._items, key=lambda k: self._items[k]["created"])]


routes_store = RouteStore()


# --------------------------------------------------------------------------- #
# Rate limiting (the site is public, and every plan fans out to third-party APIs)
# --------------------------------------------------------------------------- #

class RateLimiter:
    def __init__(self):
        self._hits: Dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_s: int = 60) -> bool:
        now = time.time()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > window_s:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            if len(self._hits) > 5000:
                self._hits.clear()
            return True


limiter = RateLimiter()


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


def throttle(request: Request, bucket: str, limit: int):
    if not limiter.allow(f"{bucket}:{client_ip(request)}", limit):
        raise HTTPException(429, "Too many requests, slow down for a minute.")


# --------------------------------------------------------------------------- #
# Background refresh: charger registry + health, once a day
# --------------------------------------------------------------------------- #

def _file_age_s(path: str) -> float:
    try:
        return time.time() - os.path.getmtime(os.path.join(ROOT, path))
    except OSError:
        return float("inf")


def maintenance_loop(stop: threading.Event):
    from check_charger_health import run_health_check
    from refresh_chargers import run_refresh

    while not stop.is_set():
        try:
            if os.environ.get("OCM_API_KEY"):
                if _file_age_s(REGISTRY_PATH) > REFRESH_INTERVAL_S:
                    print("[maintenance] refreshing charger registry")
                    run_refresh(path=os.path.join(ROOT, REGISTRY_PATH))
                if _file_age_s(HEALTH_PATH) > REFRESH_INTERVAL_S:
                    print("[maintenance] running charger health check")
                    run_health_check(os.path.join(ROOT, REGISTRY_PATH), os.path.join(ROOT, HEALTH_PATH))
        except Exception as e:  # never let a bad refresh kill the thread
            print(f"[maintenance] error: {e}")
        stop.wait(1800)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    stop = threading.Event()
    thread = threading.Thread(target=maintenance_loop, args=(stop,), daemon=True, name="marga-maintenance")
    thread.start()
    yield
    stop.set()


app = FastAPI(title="Marga", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Permissions-Policy", "geolocation=(self)")
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


class PrefixStrip:
    """ASGI middleware: treat /marga/... exactly like /..., so it works whether or not the proxy strips it."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = scope["path"]
            if path == URL_PREFIX:
                await RedirectResponse(URL_PREFIX + "/")(scope, receive, send)
                return
            if path.startswith(URL_PREFIX + "/"):
                scope = dict(scope)
                scope["path"] = path[len(URL_PREFIX):]
                scope["raw_path"] = scope["path"].encode()
        await self.inner(scope, receive, send)


# --------------------------------------------------------------------------- #
# API models
# --------------------------------------------------------------------------- #

class Point(BaseModel):
    lat: float
    lng: float


class RoutesRequest(BaseModel):
    start: Point
    end: Point


class VehicleIn(BaseModel):
    name: str = Field("Custom", max_length=80)
    battery_kwh: float = Field(ge=8, le=200)
    efficiency_wh_km: int = Field(ge=50, le=400)
    drag_coefficient: float = Field(ge=0.15, le=0.6)
    frontal_area_m2: float = Field(ge=1.4, le=4.0)
    mass_kg: float = Field(ge=500, le=4500)
    mre_pct: float = Field(ge=0.7, le=0.99)
    rolling_resistance: float = Field(0.0095, ge=0.005, le=0.02)


class PlanRequest(BaseModel):
    route_id: str = Field(max_length=32)
    vehicle: VehicleIn
    driver_style: str = "Normal"
    safety_buffer_pct: float = Field(15, ge=5, le=40)
    start_soc_pct: float = Field(100, ge=10, le=100)
    avoid_down: bool = True


def _in_india(p: Point) -> bool:
    lat_min, lat_max, lng_min, lng_max = INDIA_BOUNDS
    return lat_min <= p.lat <= lat_max and lng_min <= p.lng <= lng_max


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.get("/api/vehicles")
def api_vehicles():
    def claimed_km(text: str) -> Optional[int]:
        m = re.search(r"(\d+)\s*km", text)
        return int(m.group(1)) if m else None

    vehicles = []
    for name, spec in VEHICLE_DB.items():
        vehicles.append({
            "name": name, "battery_kwh": spec.battery_kwh, "efficiency_wh_km": spec.efficiency_wh_km,
            "claimed": spec.claimed_range, "claimed_km": claimed_km(spec.claimed_range),
            "drag_coefficient": spec.drag_coefficient, "frontal_area_m2": spec.frontal_area_m2,
            "mass_kg": spec.mass_kg, "mre_pct": spec.mre_pct, "rolling_resistance": spec.rolling_resistance,
        })
    vehicles.sort(key=lambda v: (v["name"].startswith("Custom") is False, v["name"]))
    return {"vehicles": vehicles, "driver_styles": list(DRIVER_STYLE_MULTIPLIERS)}


@app.get("/api/status")
def api_status():
    data = registry.get()
    return {
        "chargers": len(data["chargers"]),
        "registry_last_updated": data.get("registry_last_updated"),
        "health_checked": data.get("health_checked_count", 0),
        "flagged_down": data.get("down_count", 0),
        "auto_refresh": bool(os.environ.get("OCM_API_KEY")),
    }


@app.get("/api/geocode")
def api_geocode(request: Request, q: str = Query(min_length=2, max_length=80)):
    throttle(request, "geocode", 90)
    try:
        resp = requests.get(f"{GEOCODE_URL}/findAddressCandidates", params={
            "f": "json", "singleLine": q, "sourceCountry": "IND", "maxLocations": 5,
            "outFields": "Region,City", "category": "Populated Place,City,Town,Village,POI,Point Address,Street Address",
        }, timeout=8)
        resp.raise_for_status()
        candidates = resp.json().get("candidates", [])
    except Exception:
        raise HTTPException(502, "Place search is unavailable right now.")

    results, seen = [], set()
    for c in candidates:
        loc = c.get("location") or {}
        label = (c.get("address") or "").strip()
        if not label or label in seen or c.get("score", 0) < 60:
            continue
        p = Point(lat=loc.get("y", 0), lng=loc.get("x", 0))
        if not _in_india(p):
            continue
        seen.add(label)
        results.append({"label": label, "lat": p.lat, "lng": p.lng})
    return {"results": results}


@app.get("/api/reverse")
def api_reverse(request: Request, lat: float, lng: float):
    throttle(request, "geocode", 90)
    p = Point(lat=lat, lng=lng)
    if not _in_india(p):
        raise HTTPException(400, "Marga plans trips within India.")
    try:
        resp = requests.get(f"{GEOCODE_URL}/reverseGeocode", params={"f": "json", "location": f"{lng},{lat}"}, timeout=6)
        addr = resp.json().get("address", {})
        label = addr.get("City") or addr.get("Neighborhood") or addr.get("Match_addr") or f"{lat:.3f}, {lng:.3f}"
    except Exception:
        label = f"{lat:.3f}, {lng:.3f}"
    return {"label": label, "lat": lat, "lng": lng}


@app.post("/api/routes")
def api_routes(request: Request, body: RoutesRequest):
    throttle(request, "routes", 20)
    if not (_in_india(body.start) and _in_india(body.end)):
        raise HTTPException(400, "Both places need to be within India.")

    routes = get_osrm_route((body.start.lat, body.start.lng), (body.end.lat, body.end.lng))
    out = []
    for i, r in enumerate(routes):
        geometry = r["geometry"]  # [[lng, lat], ...]
        route_id = routes_store.put({"geometry": geometry, "distance_km": r["distance_km"],
                                     "duration_mins": r["duration_mins"], "fallback": r.get("fallback", False)})
        step = max(1, len(geometry) // 5000)
        drawn = [[round(pt[1], 5), round(pt[0], 5)] for pt in geometry[::step]]
        if geometry[-1] is not geometry[::step][-1]:
            drawn.append([round(geometry[-1][1], 5), round(geometry[-1][0], 5)])
        out.append({"id": route_id, "label": f"Route {i + 1}", "distance_km": r["distance_km"],
                    "duration_mins": r["duration_mins"], "fallback": r.get("fallback", False), "geometry": drawn})
    return {"routes": out}


def _route_environment(entry: Dict[str, Any]):
    """Elevation and weather for a route, fetched in parallel and cached on the route."""
    if "elevation" not in entry:
        geometry = entry["geometry"]
        key = os.environ.get("OPENWEATHER_API_KEY", "")
        with ThreadPoolExecutor(max_workers=2) as pool:
            elev_f = pool.submit(get_elevation_profile, geometry)
            wx_f = pool.submit(sample_weather_along_route, geometry, key)
            entry["elevation"] = elev_f.result()
            entry["weather"] = wx_f.result()
    return entry["elevation"], entry["weather"]


def _corridor_chargers(entry: Dict[str, Any], data: Dict[str, Any]):
    if entry.get("corridor_version") == registry.version:
        return entry["corridor"]
    geometry = entry["geometry"]
    lats = np.array([p[1] for p in geometry])
    lngs = np.array([p[0] for p in geometry])
    pad = CORRIDOR_KM / 100.0
    lat_lo, lat_hi, lng_lo, lng_hi = lats.min() - pad, lats.max() + pad, lngs.min() - pad, lngs.max() + pad
    step = max(1, len(geometry) // 1500)
    dlat, dlng = lats[::step], lngs[::step]

    corridor = []
    for c in data["chargers"]:
        a = c.get("AddressInfo", {})
        clat, clng = a.get("Latitude"), a.get("Longitude")
        if clat is None or clng is None or not (lat_lo <= clat <= lat_hi and lng_lo <= clng <= lng_hi):
            continue
        _, dist = nearest_route_point(dlat, dlng, clat, clng)
        if dist <= CORRIDOR_KM:
            corridor.append(c)
    entry["corridor"], entry["corridor_version"] = corridor, registry.version
    return corridor


def _charger_brief(c: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not c:
        return None
    a = c.get("AddressInfo", {})
    return {
        "name": a.get("Title") or "Charging station", "address": a.get("AddressLine1") or "",
        "operator": (c.get("OperatorInfo") or {}).get("Title") or "",
        "lat": a.get("Latitude"), "lng": a.get("Longitude"),
        "power_kw": c.get("max_ccs2_power", 0), "is_working": c.get("is_working", True),
        "checked": bool(c.get("health_last_checked")), "health_reason": c.get("health_reason", ""),
    }


@app.post("/api/plan")
def api_plan(request: Request, body: PlanRequest):
    throttle(request, "plan", 20)
    if body.driver_style not in DRIVER_STYLE_MULTIPLIERS:
        raise HTTPException(400, "Unknown driving style.")
    entry = routes_store.get(body.route_id)
    if not entry:
        raise HTTPException(410, "That route expired. Search again to refresh it.")

    v = body.vehicle
    spec = VehicleSpec(v.battery_kwh, v.efficiency_wh_km, "", v.drag_coefficient, v.frontal_area_m2,
                       v.mass_kg, v.mre_pct, v.rolling_resistance)
    data = registry.get()
    elevation, weather = _route_environment(entry)
    corridor = _corridor_chargers(entry, data)
    if not corridor:
        raise HTTPException(422, "No fast chargers are listed along this route yet.")

    geometry = entry["geometry"]
    duration_h = max(entry["duration_mins"], 1) / 60.0
    avg_speed = min(110.0, max(30.0, entry["distance_km"] / duration_h))

    sim = simulate_trip(geometry, spec, body.safety_buffer_pct, body.avoid_down, driver_style=body.driver_style,
                        pre_fetched_chargers=corridor, avg_speed_kmh=avg_speed, elevation_profile=elevation,
                        weather_samples=weather, start_soc_pct=body.start_soc_pct)

    battery_wh = spec.battery_kwh * 1000.0
    total_km = sim["total_distance"]
    physics_range = (battery_wh / sim["avg_wh_per_km"]) if sim["avg_wh_per_km"] > 0 else flat_baseline_range_km(spec)
    baseline_range = flat_baseline_range_km(spec)

    claimed_km = None
    known = VEHICLE_DB.get(v.name)
    if known:
        m = re.search(r"(\d+)\s*km", known.claimed_range)
        claimed_km = int(m.group(1)) if m else None

    wx_temps = [s["weather"]["temperature"] for s in weather] or [28.0]
    wx_winds = [s["weather"]["wind_speed_kmh"] for s in weather] or [0.0]
    mid = weather[len(weather) // 2]["weather"] if weather else {"description": "clear", "temperature": 28.0}
    elevation_change = (elevation[-1] - elevation[0]) if elevation else 0.0
    weather_out = {
        "description": mid.get("description", "clear"), "temp_min": min(wx_temps), "temp_max": max(wx_temps),
        "wind_kmh": max(wx_winds), "elevation_change_m": elevation_change,
        "elevation_min_m": min(elevation) if elevation else 0.0, "elevation_max_m": max(elevation) if elevation else 0.0,
    }

    explanation = AIOptimizer(os.environ.get("GEMINI_API_KEY")).explain_range_adjustment(
        baseline_range, physics_range, mid, elevation_change, body.driver_style)

    stops_out, charge_total = [], 0.0
    drive_mins_total = entry["duration_mins"]
    for i, s in enumerate(sim.get("stops", [])):
        brief = _charger_brief(s["charger"])
        charge_total += s["charge_minutes"]
        stops_out.append({
            **brief, "index": i + 1, "at_km": s["stopped_at_km"], "detour_km": s["detour_km"],
            "arrival_soc_pct": s["arrival_wh"] / battery_wh * 100.0, "depart_soc_pct": s["depart_wh"] / battery_wh * 100.0,
            "charge_minutes": s["charge_minutes"], "note": s["note"],
            "drive_eta_min": (s["stopped_at_km"] / total_km * drive_mins_total) if total_km else 0.0,
            "backup": _charger_brief(s["backup_charger"]), "backup_gap_km": s["backup_gap_km"],
        })
    running = 0.0
    for st in stops_out:
        st["eta_min"] = st["drive_eta_min"] + running
        running += st["charge_minutes"]
        del st["drive_eta_min"]

    profile = None
    if sim["status"] == "success" or sim.get("legs"):
        n = len(geometry)
        idxs = np.unique(np.linspace(0, n - 1, min(240, n)).astype(int))
        legs, km_arr, en_arr = sim["legs"], sim.get("km_by_point"), sim.get("energy_wh_by_point")
        if km_arr and en_arr:
            km_l, elev_l, soc_l, li = [], [], [], 0
            for i in idxs:
                while li + 1 < len(legs) and legs[li + 1]["start_idx"] <= i:
                    li += 1
                soc = legs[li]["start_soc"] - (en_arr[i] - legs[li]["start_wh"])
                km_l.append(round(km_arr[i], 2))
                elev_l.append(round(elevation[i], 1) if elevation else 0.0)
                soc_l.append(round(max(-5.0, soc / battery_wh * 100.0), 2))
            profile = {"km": km_l, "elev": elev_l, "soc": soc_l}

    factors = {k: v / 1000.0 for k, v in sim.get("factor_wh", {}).items()}

    corridor_out = [
        {"lat": c["AddressInfo"]["Latitude"], "lng": c["AddressInfo"]["Longitude"],
         "kw": c.get("max_ccs2_power", 0), "ok": c.get("is_working", True)}
        for c in corridor[:600]
    ]

    return {
        "status": sim["status"], "message": sim["message"],
        "summary": {
            "distance_km": total_km, "drive_mins": drive_mins_total, "charge_mins": charge_total,
            "total_mins": drive_mins_total + charge_total, "physics_range_km": physics_range,
            "baseline_range_km": baseline_range, "claimed_range_km": claimed_km,
            "avg_wh_km": sim["avg_wh_per_km"], "avg_speed_kmh": avg_speed,
            "arrival_soc_pct": sim.get("arrival_soc_pct"), "trip_energy_kwh": sim["total_energy_wh"] / 1000.0,
            "stops": len(stops_out),
        },
        "factors_kwh": factors, "weather": weather_out, "explanation": explanation,
        "stops": stops_out, "profile": profile, "chargers": corridor_out,
        "registry": {"last_updated": data.get("registry_last_updated"),
                     "health_checked": data.get("health_checked_count", 0), "flagged_down": data.get("down_count", 0)},
    }


@app.get("/api/weather-now")
def api_weather_now(request: Request, lat: float, lng: float):
    throttle(request, "wx", 30)
    return get_weather(lat, lng, os.environ.get("OPENWEATHER_API_KEY", ""))


# --------------------------------------------------------------------------- #
# Frontend
# --------------------------------------------------------------------------- #

def _index_response() -> HTMLResponse:
    with open(os.path.join(WEB_DIR, "index.html"), "r", encoding="utf-8") as f:
        html = f.read()
    boot = re.search(r'<script id="boot">(.*?)</script>', html, re.S)
    script_hash = ""
    if boot:
        digest = hashlib.sha256(boot.group(1).encode()).digest()
        script_hash = f" 'sha256-{base64.b64encode(digest).decode()}'"
    csp = (
        "default-src 'self'; "
        f"script-src 'self'{script_hash}; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https://server.arcgisonline.com; "
        "connect-src 'self'; font-src 'self'; base-uri 'self'; form-action 'none'; frame-ancestors 'none'"
    )
    return HTMLResponse(html, headers={"Content-Security-Policy": csp, "Cache-Control": "no-cache"})


@app.get("/", include_in_schema=False)
def index():
    return _index_response()


app.mount("/", StaticFiles(directory=WEB_DIR), name="web")

asgi_app = PrefixStrip(app)
