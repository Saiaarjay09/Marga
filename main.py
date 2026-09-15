import dataclasses

import requests
import streamlit as st
import pydeck as pdk
from streamlit_geolocation import streamlit_geolocation

from data.vehicles import VEHICLE_DB
from core.geo import haversine_km
from core.physics import flat_baseline_range_km
from core.elevation import get_elevation_profile
from core.charger_registry import get_registry_with_health
from core.simulator import simulate_trip
from core.ai_optimizer import AIOptimizer
from api.routing import get_coords_from_city, get_osrm_route
from api.weather import get_weather, sample_weather_along_route
from api.chargers import OpenChargeMapClient

CORRIDOR_KM = 10.0
DRIVER_STYLES = ["Eco", "Normal", "Aggressive"]


@st.cache_data(show_spinner=False)
def cached_geocode(city_name: str):
    """Cached wrapper around ArcGIS geocoder — never looks up the same city twice."""
    return get_coords_from_city(city_name)


@st.cache_data(show_spinner=False, ttl=3600)
def cached_registry():
    """Local self-updating registry + daily health-check overlay, cached for an hour per session."""
    return get_registry_with_health()


@st.cache_data(show_spinner=False, ttl=900)
def cached_elevation_profile(route_geometry_tuple):
    return get_elevation_profile([list(pt) for pt in route_geometry_tuple])


@st.cache_data(show_spinner=False, ttl=900)
def cached_weather_samples(route_geometry_tuple, owm_key):
    return sample_weather_along_route([list(pt) for pt in route_geometry_tuple], owm_key)


def chargers_on_corridor(all_chargers, route_geometry, corridor_km=CORRIDOR_KM):
    on_route = []
    for charger in all_chargers:
        addr = charger.get("AddressInfo", {})
        c_lat, c_lng = addr.get("Latitude"), addr.get("Longitude")
        if c_lat is None or c_lng is None:
            continue
        min_dist = min(haversine_km(pt[1], pt[0], c_lat, c_lng) for pt in route_geometry)
        if min_dist <= corridor_km:
            power = 50
            conns = charger.get("Connections", [])
            if conns:
                power = max((c.get("PowerKW") or 0) for c in conns) or 50
            charger["max_ccs2_power"] = power
            on_route.append(charger)
    return on_route


# --- UI Configuration ---
st.set_page_config(page_title="EV Trip Planner", layout="wide")
st.title("⚡ AI-Driven EV Trip Planner")

# --- Sidebar Controls ---
with st.sidebar:
    st.header("Trip Settings")

    try:
        gemini_api_key = st.secrets.get("GEMINI_API_KEY", "")
        weather_api_key = st.secrets.get("OPENWEATHER_API_KEY", "")
        ocm_api_key = st.secrets.get("OCM_API_KEY", "")
    except FileNotFoundError:
        st.warning("Secrets file not found. Please set your Streamlit secrets.")
        gemini_api_key = weather_api_key = ocm_api_key = ""

    st.subheader("📍 Start Location")
    gps_col, city_col = st.columns([1, 3])
    with gps_col:
        location = streamlit_geolocation()
    with city_col:
        if location and location.get("latitude") is not None and location.get("longitude") is not None:
            gps_lat, gps_lon = location["latitude"], location["longitude"]
            try:
                rev_url = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/reverseGeocode?f=json&location={gps_lon},{gps_lat}"
                rev_resp = requests.get(rev_url, timeout=5)
                gps_city = rev_resp.json().get("address", {}).get("City", f"{gps_lat:.4f}, {gps_lon:.4f}")
            except Exception:
                gps_city = f"{gps_lat:.4f}, {gps_lon:.4f}"
            st.session_state["gps_start"] = (gps_lat, gps_lon)
            start_city = st.text_input("Start City", value=gps_city)
        else:
            start_city = st.text_input("Start City", value="Bengaluru")

    end_city = st.text_input("Destination City", value="Goa")

    st.markdown("---")
    st.sidebar.title("🚗 Vehicle Profile Settings")

    selected_vehicle = st.sidebar.selectbox("Select EV Model Preset Profile", sorted(VEHICLE_DB.keys()))
    is_custom = selected_vehicle == "Custom / Manual Profile"
    base_spec = VEHICLE_DB[selected_vehicle]

    battery_capacity = st.sidebar.slider(
        "Battery Capacity (Usable kWh)", 10.0, 120.0, float(base_spec.battery_kwh),
        help="Usable energy your vehicle can draw from full to empty.",
    )
    efficiency = st.sidebar.slider(
        "Flat Baseline Efficiency (Wh/km)", 50, 300, int(base_spec.efficiency_wh_km),
        help="Legacy flat efficiency, shown only as a sanity-check baseline — the physics model below drives the real estimate.",
    )

    with st.sidebar.expander("Physics parameters (drag, mass, drivetrain)", expanded=is_custom):
        drag_coefficient = st.slider("Drag Coefficient (Cd)", 0.18, 0.45, float(base_spec.drag_coefficient), 0.01)
        frontal_area = st.slider("Frontal Area (m²)", 1.8, 3.2, float(base_spec.frontal_area_m2), 0.05)
        mass_kg = st.slider("Mass incl. driver (kg)", 700, 3200, int(base_spec.mass_kg), 25)
        mre_pct = st.slider(
            "Motor + Inverter Rated Efficiency — MRE (%)", 80, 96, int(base_spec.mre_pct * 100),
            help="Average electric-drivetrain efficiency: how much of the battery's energy reaches the wheels.",
        ) / 100.0

    vehicle_spec = dataclasses.replace(
        base_spec,
        battery_kwh=battery_capacity,
        efficiency_wh_km=efficiency,
        drag_coefficient=drag_coefficient,
        frontal_area_m2=frontal_area,
        mass_kg=mass_kg,
        mre_pct=mre_pct,
    )

    if not is_custom:
        st.sidebar.info(f"**Official Claimed Range:** {base_spec.claimed_range}")

    driver_style = st.sidebar.select_slider("Driving Style", options=DRIVER_STYLES, value="Normal")
    reliability_toggle = st.toggle("Avoid Chargers Currently Flagged as Down", value=True)
    safety_buffer = st.slider("Battery Safety Buffer (%)", min_value=5, max_value=30, value=15, step=1)

    baseline_range_km = round(flat_baseline_range_km(vehicle_spec), 1)
    st.info(f"💡 Flat-efficiency baseline range: **{baseline_range_km} km** (physics-adjusted estimate appears after routing)")

    search_routes_button = st.button("🔍 Search Routes", use_container_width=True)

# --- Step 1: Fetch Routes ---
if search_routes_button:
    for key in ("routes", "selected_route", "simulation_result", "chargers"):
        st.session_state.pop(key, None)

    if not start_city or not end_city:
        st.warning("Please enter both a start point and a destination.")
    else:
        with st.spinner("Resolving cities and fetching route..."):
            start_lat, start_lng = cached_geocode(start_city)
            end_lat, end_lng = cached_geocode(end_city)

            if start_lat is None or end_lat is None:
                st.error("Could not resolve one or both cities within India.")
                st.stop()

            st.session_state["start_coords"] = (start_lat, start_lng)
            st.session_state["end_coords"] = (end_lat, end_lng)

            try:
                routes = get_osrm_route(st.session_state["start_coords"], st.session_state["end_coords"])
                if routes:
                    st.session_state["routes"] = routes
                    st.session_state["selected_route"] = routes[0]
                    st.success("Route loaded. Pick a route below, then search for chargers.")
                    st.rerun()
                else:
                    st.error("No valid driving routes found.")
            except Exception as e:
                st.error(f"Execution failed: {e}")
                st.stop()

# --- Step 2: Display Route Options ---
if "routes" in st.session_state and st.session_state["routes"]:
    routes = st.session_state["routes"]
    st.subheader("🛣️ Available Routes")

    route_labels = []
    for r in routes:
        hours, mins = int(r["duration_mins"] // 60), int(r["duration_mins"] % 60)
        time_str = f"{hours} hr {mins} min" if hours > 0 else f"{mins} min"
        label = f"{r['route_id']}: {r['distance_km']} km | Est. Time: {time_str}"
        if r.get("fallback"):
            label += " ⚠️ (Geometric Fallback)"
        route_labels.append(label)

    selected_label = st.radio("Select a route:", route_labels, index=0)
    selected_route = routes[route_labels.index(selected_label)]

    sel_hours, sel_mins = int(selected_route["duration_mins"] // 60), int(selected_route["duration_mins"] % 60)
    sel_time_str = f"{sel_hours} hr {sel_mins} min" if sel_hours > 0 else f"{sel_mins} min"
    st.info(f"📍 Selected: **{selected_route['route_id']}** — {selected_route['distance_km']} km, ~{sel_time_str}")

    find_chargers_button = st.button("⚡ Find Chargers for Selected Route", use_container_width=True)

    if find_chargers_button:
        route_geometry = selected_route["geometry"]
        start_lat, start_lng = st.session_state["start_coords"]
        end_lat, end_lng = st.session_state["end_coords"]

        with st.spinner("Fetching elevation, weather, and live charger data, then running the physics model..."):
            geometry_tuple = tuple(tuple(pt) for pt in route_geometry)
            elevation_profile = cached_elevation_profile(geometry_tuple)
            weather_samples = cached_weather_samples(geometry_tuple, weather_api_key)

            registry = cached_registry()
            all_chargers = {c.get("ID"): c for c in registry["chargers"]}

            if ocm_api_key:
                live_client = OpenChargeMapClient(ocm_api_key)
                for c in live_client.get_chargers_along_route(route_geometry, corridor_km=CORRIDOR_KM):
                    all_chargers.setdefault(c.get("ID"), c)

            chargers_on_route = chargers_on_corridor(list(all_chargers.values()), route_geometry)

            avg_speed = (selected_route["distance_km"] / (selected_route["duration_mins"] / 60.0)
                         if selected_route["duration_mins"] > 0 else 60)

            if not chargers_on_route:
                st.warning("⚠️ No compatible DC fast chargers detected on this route.")
                st.info(
                    "💡 Your local registry may be stale for this corridor. Run `python refresh_chargers.py` "
                    "to pull the latest national charger listing, and `python check_charger_health.py` to "
                    "refresh which stations are currently reported working, then commit the updated files."
                )
                st.stop()

            result = simulate_trip(
                route_geometry, vehicle_spec, safety_buffer, reliability_toggle,
                driver_style=driver_style, pre_fetched_chargers=chargers_on_route,
                avg_speed_kmh=avg_speed, elevation_profile=elevation_profile, weather_samples=weather_samples,
            )

            mid_weather = get_weather(
                (start_lat + end_lat) / 2, (start_lng + end_lng) / 2, weather_api_key
            )
            elevation_change = (elevation_profile[-1] - elevation_profile[0]) if elevation_profile else 0.0
            physics_range_km = (
                battery_capacity * 1000.0 / result["avg_wh_per_km"]
                if result.get("avg_wh_per_km") else baseline_range_km
            )

            optimizer = AIOptimizer(gemini_api_key)
            explanation = optimizer.explain_range_adjustment(
                baseline_range_km, physics_range_km, mid_weather, elevation_change, driver_style
            )

            st.session_state["simulation_result"] = result
            st.session_state["selected_geometry"] = route_geometry
            st.session_state["physics_range_km"] = physics_range_km
            st.session_state["range_explanation"] = explanation
            st.session_state["registry_meta"] = registry

# --- Render Results ---
if "simulation_result" in st.session_state and st.session_state["simulation_result"]:
    result = st.session_state["simulation_result"]
    route_geometry = st.session_state["selected_geometry"]
    start_lat, start_lng = st.session_state["start_coords"]
    end_lat, end_lng = st.session_state["end_coords"]

    st.subheader("🔋 Physics-Based Range Estimate")
    col1, col2 = st.columns(2)
    col1.metric("Flat baseline (old-style)", f"{baseline_range_km:.0f} km")
    col2.metric("Physics-adjusted for this trip", f"{st.session_state['physics_range_km']:.0f} km")
    st.caption(st.session_state["range_explanation"])

    registry_meta = st.session_state.get("registry_meta", {})
    last_updated = registry_meta.get("registry_last_updated") or "unknown (run refresh_chargers.py)"
    st.caption(f"Charger registry last refreshed: {last_updated} · "
               f"{registry_meta.get('health_checked_count', 0)} stations health-checked.")

    if result["status"] == "success":
        st.success(f"{result['message']} Stops required: {len(result['stops'])}")

        if result.get("stops"):
            waypoints_parts = []
            for stop in result["stops"]:
                c_addr = stop["charger"].get("AddressInfo", {})
                wp_lat, wp_lon = c_addr.get("Latitude"), c_addr.get("Longitude")
                if wp_lat and wp_lon:
                    waypoints_parts.append(f"{wp_lat},{wp_lon}")

            google_maps_url = (
                f"https://www.google.com/maps/dir/?api=1&origin={start_lat},{start_lng}"
                f"&destination={end_lat},{end_lng}&waypoints={'|'.join(waypoints_parts)}"
            )
            st.markdown(
                f'<a href="{google_maps_url}" target="_blank">'
                f'<button style="background-color:#1E88E5;color:white;padding:10px 20px;'
                f'border-radius:5px;border:none;cursor:pointer;font-size:16px;">'
                f'🗺️ Send to Phone (Open in Google Maps)</button></a>',
                unsafe_allow_html=True,
            )

            st.markdown("#### Planned Stops")
            for idx, stop in enumerate(result["stops"]):
                addr = stop["charger"].get("AddressInfo", {})
                health_icon = "🟢" if stop["is_working"] else "🟠"
                line = (f"{health_icon} **Stop {idx + 1}: {addr.get('Title', 'Unknown')}** — "
                        f"{stop['power_kw']:.0f} kW, at {stop['stopped_at_km']:.0f} km. {stop['note']}")
                st.markdown(line)
                if stop.get("backup_charger"):
                    b_addr = stop["backup_charger"].get("AddressInfo", {})
                    st.caption(f"　↳ Backup option: {b_addr.get('Title', 'Unknown')} "
                               f"({stop['backup_charger'].get('max_ccs2_power', 0):.0f} kW)")
    else:
        st.error(f"Trip failed: {result['message']}")

    # --- Map ---
    layers = []
    ROUTE_COLOR = [30, 60, 150, 255]       # navy blue — primary route
    ALT_ROUTE_COLOR = [150, 150, 150, 120]  # grey — alternates
    PRIMARY_STOP_COLOR = [255, 140, 0]      # orange — chosen charging stop
    BACKUP_STOP_COLOR = [255, 193, 7]       # amber/gold — backup option
    DOWN_STOP_COLOR = [120, 120, 120]       # grey — flagged not working, used only as last resort

    if st.session_state.get("routes"):
        for r in st.session_state["routes"]:
            if r["route_id"] != st.session_state["selected_route"]["route_id"]:
                layers.append(pdk.Layer("PathLayer", [{"path": r["geometry"]}], get_path="path",
                                         get_color=ALT_ROUTE_COLOR, width_scale=20, width_min_pixels=3))
        layers.append(pdk.Layer("PathLayer", [{"path": st.session_state["selected_route"]["geometry"]}],
                                 get_path="path", get_color=ROUTE_COLOR, width_scale=20, width_min_pixels=5))
    else:
        layers.append(pdk.Layer("PathLayer", [{"path": route_geometry}], get_path="path",
                                 get_color=ROUTE_COLOR, width_scale=20, width_min_pixels=5))

    if result.get("stops"):
        primary_data, backup_data = [], []
        for idx, stop in enumerate(result["stops"]):
            addr = stop["charger"].get("AddressInfo", {})
            color = DOWN_STOP_COLOR if not stop["is_working"] else PRIMARY_STOP_COLOR
            primary_data.append({
                "name": f"Stop {idx + 1}: {addr.get('Title', 'Unknown')}",
                "coordinates": [addr.get("Longitude"), addr.get("Latitude")],
                "power": stop["power_kw"], "dist": stop["stopped_at_km"],
                "status": "Working" if stop["is_working"] else "⚠️ Flagged down",
                "color": color,
            })
            if stop.get("backup_charger"):
                b_addr = stop["backup_charger"].get("AddressInfo", {})
                backup_data.append({
                    "name": f"Backup for Stop {idx + 1}: {b_addr.get('Title', 'Unknown')}",
                    "coordinates": [b_addr.get("Longitude"), b_addr.get("Latitude")],
                    "power": stop["backup_charger"].get("max_ccs2_power", 0), "dist": stop["stopped_at_km"],
                    "status": "Backup option",
                })

        layers.append(pdk.Layer(
            "ScatterplotLayer", primary_data, pickable=True, opacity=0.9, stroked=True, filled=True,
            radius_min_pixels=8, radius_max_pixels=100, line_width_min_pixels=2,
            get_position="coordinates", get_radius=1500, get_fill_color="color",
            get_line_color=[255, 255, 255],
        ))
        if backup_data:
            layers.append(pdk.Layer(
                "ScatterplotLayer", backup_data, pickable=True, opacity=0.75, stroked=True, filled=True,
                radius_min_pixels=6, radius_max_pixels=80, line_width_min_pixels=2,
                get_position="coordinates", get_radius=1200, get_fill_color=BACKUP_STOP_COLOR,
                get_line_color=[255, 255, 255],
            ))

    view_state = pdk.ViewState(latitude=(start_lat + end_lat) / 2, longitude=(start_lng + end_lng) / 2, zoom=5, pitch=30)
    st.pydeck_chart(pdk.Deck(
        layers=layers, initial_view_state=view_state,
        tooltip={"text": "{name}\nPower: {power} kW\nStatus: {status}\nStop at: {dist} km"},
    ))
else:
    view_state = pdk.ViewState(latitude=15.0, longitude=76.0, zoom=5)
    st.pydeck_chart(pdk.Deck(initial_view_state=view_state))
