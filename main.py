import requests
import streamlit as st
import pydeck as pdk
from streamlit_geolocation import streamlit_geolocation
from api.chargers import OpenChargeMapClient
from core.simulator import Simulator
from api.routing import get_coords_from_city, get_osrm_route
from core.ai_optimizer import AIOptimizer
from api.charging import get_charging_stations

@st.cache_data(show_spinner=False)
def cached_geocode(city_name: str):
    """Cached wrapper around ArcGIS geocoder — never looks up the same city twice."""
    return get_coords_from_city(city_name)

# --- UI Configuration ---
st.set_page_config(page_title="EV Trip Planner", layout="wide")
st.title("⚡ AI-Driven EV Trip Planner")

# --- Sidebar Controls ---
with st.sidebar:
    st.header("Trip Settings")
    
    try:
        ocm_api_key = st.secrets["OCM_API_KEY"]
        gemini_api_key = st.secrets.get("GEMINI_API_KEY", "")
        weather_api_key = st.secrets.get("OPENWEATHER_API_KEY", "")
    except FileNotFoundError:
        st.warning("Secrets file not found. Please set your Streamlit secrets.")
        ocm_api_key = ""
        gemini_api_key = ""
        weather_api_key = ""
        
    st.subheader("📍 Start Location")
    gps_col, city_col = st.columns([1, 3])
    with gps_col:
        location = streamlit_geolocation()
    with city_col:
        # If GPS detected, reverse-geocode to fill the city name
        if location and location.get("latitude") is not None and location.get("longitude") is not None:
            gps_lat = location["latitude"]
            gps_lon = location["longitude"]
            # Reverse geocode using ArcGIS
            try:
                rev_url = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/reverseGeocode?f=json&location={gps_lon},{gps_lat}"
                rev_resp = requests.get(rev_url, timeout=5)
                rev_data = rev_resp.json()
                gps_city = rev_data.get("address", {}).get("City", f"{gps_lat:.4f}, {gps_lon:.4f}")
            except Exception:
                gps_city = f"{gps_lat:.4f}, {gps_lon:.4f}"
            st.session_state["gps_start"] = (gps_lat, gps_lon)
            start_city = st.text_input("Start City", value=gps_city)
        else:
            start_city = st.text_input("Start City", value="Bengaluru")
    
    end_city = st.text_input("Destination City", value="Goa")
    
    st.markdown("---")
    st.header("🚗 EV Configuration")
    battery_capacity = st.number_input("Battery Capacity (kWh)", min_value=10.0, max_value=150.0, value=60.0, step=1.0)
    wh_per_km = st.number_input("Efficiency (Wh/km)", min_value=50, max_value=400, value=150, step=5)
    reliability_toggle = st.toggle("Show Only High-Reliability Stations", value=True)
    safety_buffer = st.slider("Battery Safety Buffer (%)", min_value=5, max_value=30, value=15, step=1)
    
    # Dynamically compute range based on user inputs
    usable_range_km = round((battery_capacity * 1000) / wh_per_km, 1)
    st.info(f"💡 Calculated Real-World Range: **{usable_range_km} km**")
    
    search_routes_button = st.button("🔍 Search Routes", use_container_width=True)

# --- Step 1: Fetch Routes ---
if search_routes_button:
    st.session_state.pop('routes', None)
    st.session_state.pop('selected_route', None)
    st.session_state.pop('itinerary', None)
    st.session_state.pop('simulation_result', None)
    st.session_state.pop('chargers', None)
    
    if not ocm_api_key:
        st.warning("Please configure your Open Charge Map API Key in Streamlit Secrets.")
    elif not start_city or not end_city:
        st.warning("Please enter both a start point and a destination.")
    else:
        with st.spinner("Processing route and loading verified charging networks..."):
            start_lat, start_lng = get_coords_from_city(start_city)
            end_lat, end_lng = get_coords_from_city(end_city)
            
            if start_lat is None or end_lat is None:
                st.error("Could not resolve one or both cities within India.")
                st.stop()
            
            st.session_state["start_coords"] = (start_lat, start_lng)
            st.session_state["end_coords"] = (end_lat, end_lng)
                
            try:
                # Get the verified highway route
                routes = get_osrm_route(st.session_state["start_coords"], st.session_state["end_coords"])
                
                if routes:
                    st.session_state["routes"] = routes
                    st.session_state["selected_route"] = routes[0]
                    
                    # Call charger engine using the custom user range!
                    chargers = get_charging_stations(start_lat, start_lng, usable_range_km, ocm_api_key)
                    st.session_state["chargers"] = chargers
                    
                    st.success("Route and verified chargers loaded successfully!")
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
    
    # Build radio labels with human-readable duration
    route_labels = []
    for r in routes:
        duration_mins = r['duration_mins']
        hours = int(duration_mins // 60)
        mins = int(duration_mins % 60)
        time_str = f"{hours} hr {mins} min" if hours > 0 else f"{mins} min"
        
        label = f"{r['route_id']}: {r['distance_km']} km | Est. Time: {time_str}"
        if r.get("fallback"):
            label += " ⚠️ (Geometric Fallback)"
        route_labels.append(label)
    
    selected_label = st.radio("Select a route:", route_labels, index=0)
    selected_index = route_labels.index(selected_label)
    selected_route = routes[selected_index]
    
    sel_hours = int(selected_route['duration_mins'] // 60)
    sel_mins = int(selected_route['duration_mins'] % 60)
    sel_time_str = f"{sel_hours} hr {sel_mins} min" if sel_hours > 0 else f"{sel_mins} min"
    st.info(f"📍 Selected: **{selected_route['route_id']}** — {selected_route['distance_km']} km, ~{sel_time_str}")
    
    # --- Step 3: Find Chargers for Selected Route ---
    find_chargers_button = st.button("⚡ Find Chargers for Selected Route", use_container_width=True)
    
    if find_chargers_button:
        route_geometry = selected_route["geometry"]
        start_lat, start_lng = st.session_state["start_coords"]
        end_lat, end_lng = st.session_state["end_coords"]
        
        with st.spinner("Running AI Optimizer and finding dynamic chargers..."):
            # Initialize AI Optimizer
            optimizer = AIOptimizer(gemini_api_key, weather_api_key)
            ai_insights = optimizer.analyze_segment_efficiency(start_lat, start_lng, end_lat, end_lng, usable_range_km)
            
            st.success(f"🤖 AI Reasoning: {ai_insights['reasoning']}")
            
            # Adjust range based on AI modifier
            adjusted_range = usable_range_km * (1 + ai_insights['modifier'] / 100.0)
            st.info(f"🔋 AI-adjusted range: {adjusted_range:.1f} km")
            
            # Real-World Physics Penalties
            HIGHWAY_SPEED_PENALTY = 0.75   # 100+ km/h highway speeds + heavy AC usage
            GHATS_ELEVATION_PENALTY = 0.85 # Western Ghats climb penalty for routes into Goa
            
            adjusted_range = adjusted_range * HIGHWAY_SPEED_PENALTY * GHATS_ELEVATION_PENALTY
            st.warning(f"⚠️ Real-world range after highway & Ghats penalties: {adjusted_range:.1f} km (×0.75 speed, ×0.85 elevation)")
            
            client = OpenChargeMapClient(api_key=ocm_api_key)
            
            # Dynamic Charger Discovery: Route Snapping
            all_chargers_on_route = client.get_chargers_along_route(
                route_geometry, 
                corridor_km=10, 
                require_recent_checkin=reliability_toggle
            )
            
            # Filter chargers using AI Confidence Score > 85%
            high_confidence_chargers = optimizer.filter_high_confidence_chargers(all_chargers_on_route)
            
            # Run simulation using real geometry waypoints and pre-fetched chargers
            simulator = Simulator(client)
            # The simulator needs [(lat, lng)]
            route_tuples = [(pt[1], pt[0]) for pt in route_geometry]
            
            # Convert safety buffer percentage to km
            safety_buffer_km = usable_range_km * (safety_buffer / 100.0)
            
            result = simulator.simulate_trip(
                route_tuples,
                adjusted_range,
                safety_buffer_km,
                reliability_toggle,
                pre_fetched_chargers=high_confidence_chargers
            )
            
            # Store result for rendering
            st.session_state["simulation_result"] = result
            st.session_state["selected_geometry"] = route_geometry

# --- Render Map & Results ---
if "simulation_result" in st.session_state and st.session_state["simulation_result"]:
    result = st.session_state["simulation_result"]
    route_geometry = st.session_state["selected_geometry"]
    start_lat, start_lng = st.session_state["start_coords"]
    end_lat, end_lng = st.session_state["end_coords"]
    
    if result["status"] == "success":
        st.success(f"{result['message']} Stops required: {len(result['stops'])}")
        
        # --- Google Maps Deep-Link Exporter ---
        if result.get("stops"):
            waypoints_parts = []
            for stop in result["stops"]:
                c_addr = stop["charger"].get("AddressInfo", {})
                wp_lat = c_addr.get("Latitude")
                wp_lon = c_addr.get("Longitude")
                if wp_lat and wp_lon:
                    waypoints_parts.append(f"{wp_lat},{wp_lon}")
            
            waypoints_str = "|".join(waypoints_parts)
            google_maps_url = (
                f"https://www.google.com/maps/dir/?api=1"
                f"&origin={start_lat},{start_lng}"
                f"&destination={end_lat},{end_lng}"
                f"&waypoints={waypoints_str}"
            )
            
            st.markdown(
                f'<a href="{google_maps_url}" target="_blank">'
                f'<button style="background-color:#4CAF50;color:white;padding:10px 20px;'
                f'border-radius:5px;border:none;cursor:pointer;font-size:16px;">'
                f'🗺️ Send to Phone (Open in Google Maps)</button></a>',
                unsafe_allow_html=True
            )
    else:
        st.error(f"Trip failed: {result['message']}")
        
    # Render Map with PyDeck
    layers = []
    
    # 1. High-fidelity Route Layer
    if "routes" in st.session_state and st.session_state.get("selected_route"):
        # Draw alternative routes beneath the primary route
        for r in st.session_state["routes"]:
            if r["route_id"] != st.session_state["selected_route"]["route_id"]:
                layers.append(
                    pdk.Layer(
                        "PathLayer",
                        [{"path": r["geometry"]}],
                        get_path="path",
                        get_color=[150, 150, 150, 120],
                        width_scale=20,
                        width_min_pixels=3,
                    )
                )
        
        # Draw the primary selected route prominently on top
        layers.append(
            pdk.Layer(
                "PathLayer",
                [{"path": st.session_state["selected_route"]["geometry"]}],
                get_path="path",
                get_color=[0, 120, 255, 255],
                width_scale=20,
                width_min_pixels=5,
            )
        )
    else:
        # Fallback route rendering
        route_data = [{"path": route_geometry}]
        layers.append(
            pdk.Layer(
                "PathLayer",
                route_data,
                get_path="path",
                get_color=[0, 120, 255, 255],
                width_scale=20,
                width_min_pixels=5,
            )
        )
    
    # 2. Charging Stops Layer
    if result.get("stops"):
        stops_data = []
        for idx, stop in enumerate(result["stops"]):
            charger = stop["charger"]
            addr = charger.get("AddressInfo", {})
            stops_data.append({
                "name": f"Stop {idx+1}: {addr.get('Title', 'Unknown')}",
                "coordinates": [addr.get("Longitude"), addr.get("Latitude")],
                "power": stop["power_kw"],
                "score": charger.get("ai_confidence_score", "N/A"),
                "dist": stop["stopped_at_km"]
            })
            
        layers.append(
            pdk.Layer(
                "ScatterplotLayer",
                stops_data,
                pickable=True,
                opacity=0.9,
                stroked=True,
                filled=True,
                radius_scale=6,
                radius_min_pixels=8,
                radius_max_pixels=100,
                line_width_min_pixels=2,
                get_position="coordinates",
                get_radius=1500, # 1.5km radius for visibility
                get_fill_color=[76, 175, 80], # Green
                get_line_color=[255, 255, 255],
            )
        )

    view_state = pdk.ViewState(
        latitude=(start_lat + end_lat) / 2,
        longitude=(start_lng + end_lng) / 2,
        zoom=5,
        pitch=30,
    )
    
    r = pdk.Deck(
        layers=layers,
        initial_view_state=view_state,
        tooltip={"text": "{name}\nPower: {power} kW\nAI Score: {score}%\nStop at: {dist} km"}
    )
    
    st.pydeck_chart(r)
else:
    # Default Empty Map
    view_state = pdk.ViewState(latitude=15.0, longitude=76.0, zoom=5)
    st.pydeck_chart(pdk.Deck(initial_view_state=view_state))
