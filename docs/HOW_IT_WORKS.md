# How Marga works

Marga (मार्ग, "path") plans electric-car road trips in India. Instead of trusting the brochure range, it simulates
your exact car over your exact road, one small stretch at a time, using real elevation, real weather and real charger
data, then plans the charging stops (each with a backup) and skips stations that are reported broken.

This document explains the whole system and then walks through **every file** in the repository.

- [The big picture](#the-big-picture)
- [What happens when you press "Plan my trip"](#what-happens-when-you-press-plan-my-trip)
- [The physics model](#the-physics-model)
- [The trip simulator](#the-trip-simulator)
- [The charger data pipeline](#the-charger-data-pipeline)
- [Security and abuse protection](#security-and-abuse-protection)
- [File by file](#file-by-file)
- [Running, testing and deploying](#running-testing-and-deploying)
- [Known limits](#known-limits)

---

## The big picture

```
 Browser (web/)                         Server (server.py, FastAPI)                  Outside world
┌───────────────────────┐   JSON      ┌──────────────────────────────┐
│ index.html + css + js │ ──────────► │ /api/geocode  ───────────────┼──►  ArcGIS geocoder (place search)
│  · form, car picker   │             │ /api/routes   ───────────────┼──►  OSRM (road routes)
│  · Leaflet map        │ ◄────────── │ /api/plan                    │
│  · battery chart      │   results   │    ├─ core/elevation ────────┼──►  Open-Meteo (elevation)
└───────────────────────┘             │    ├─ api/weather ───────────┼──►  OpenWeatherMap / Open-Meteo
                                      │    ├─ core/charger_registry  │     (weather + wind direction)
                                      │    ├─ core/simulator         │
                                      │    │    └─ core/physics      │
                                      │    └─ core/ai_optimizer ─────┼──►  Gemini (optional wording)
                                      │                              │
                                      │ background thread, daily:    │
                                      │   refresh_chargers.py ───────┼──►  Open Charge Map (all Indian chargers)
                                      │   check_charger_health.py ───┼──►  Open Charge Map (status + check-ins)
                                      └──────────────────────────────┘
                                         every_charger_india.json
                                         charger_health.json
```

One Python process (`server.py`) serves both the JSON API and the static website. There is no database: the charger
list and its health results are two JSON files on disk, and routes are kept in memory for an hour.

The site is hosted on the author's Mac and published to the internet with **Tailscale Funnel** at
`https://haven.taila6d3cb.ts.net/marga/`. A macOS LaunchAgent keeps the server running.

---

## What happens when you press "Plan my trip"

1. **Places.** If you typed a place but did not pick a suggestion, the browser asks `/api/geocode` and takes the top
   match. Each suggestion is an ArcGIS result limited to India.
2. **Routes.** The browser posts the two coordinates to `/api/routes`. The server asks OSRM for up to three driving
   routes, stores each route's full geometry in memory under a random id, and sends back a (thinned) copy to draw.
3. **Plan.** The browser posts the route id plus your car, driving style, start charge and buffer to `/api/plan`.
   The server then:
   1. Fetches the **elevation profile** and **weather along the route** in parallel (results are cached on the route).
   2. Finds every **fast charger within 10 km of the route**, using the charger registry with its health overlay.
   3. Runs the **simulator**, which walks the route in small steps computing energy use, and decides where to stop.
   4. Builds the response: range comparison, itinerary with backups, battery-level graph, a breakdown of where the
      energy went, weather summary, and registry freshness.
4. **Render.** The browser draws the route, stops, backups and nearby chargers on the map, and fills the panel.

Typical time is 3-6 seconds, dominated by the weather and elevation lookups.

---

## The physics model

File: `core/physics.py`. For every short segment of the route it balances the forces on the car:

| Force | Formula | What it captures |
|---|---|---|
| Air drag | `½ · ρ · Cd · A · v²` | Speed, your car's drag coefficient and frontal area. `v` includes headwind/tailwind. |
| Rolling resistance | `Crr · m · g · cos θ` | Tyres and road, grows with weight. |
| Grade | `m · g · sin θ` | Climbing costs energy; descending gives it back. |

- **Air density** falls with altitude (`ρ = 1.225 · e^(−h/8434)`), so thin mountain air means less drag.
- **Wind.** Each segment has a compass bearing. The wind's "blowing from" direction is projected onto it:
  `headwind = wind_speed · cos(wind_from − bearing)`. A tailwind is a negative headwind.
- **Drivetrain.** Mechanical work is divided by the car's motor+inverter efficiency (the "MRE", ~86-93%) to get
  energy drawn from the battery.
- **Regeneration.** When the net force is negative (long descents), energy is recovered at 65% efficiency, but capped
  at 70 kW because real regen systems cannot absorb more; the rest is lost as brake heat.
- **Climate load.** Accessories draw 0.3 kW. Below 15 °C, heating adds 0.15 kW per degree (max 6 kW). Above 28 °C,
  cooling adds 0.25 kW per degree (max 3.5 kW). Multiplied by time spent driving.
- **Driving style.** Propulsion energy is multiplied by 0.93 (Eco), 1.00 (Normal) or 1.14 (Spirited).

`segment_energy_parts()` returns the energy split into six parts that always sum to the total: **air resistance,
wind, tyres and road, hills, climate, driving style**. Those are the bars in the "Where your kWh goes" card.

The "real range" number is `battery_kWh ÷ average Wh/km over this route`, i.e. what a full battery would cover on
this exact road in these conditions.

---

## The trip simulator

File: `core/simulator.py`. It works in **energy (Wh)**, not kilometres, because energy per km changes with hills and
wind.

1. **Energy profile.** For each pair of consecutive route points it computes the segment energy with the physics
   model and keeps a running total, so every point on the route has "energy used so far".
2. **Project chargers onto the route.** Each nearby charger is matched to its closest route point (vectorised with
   NumPy), giving its position along the route, its distance off-route, and a detour cost (there-and-back at the
   car's flat Wh/km).
3. **Walk forward.** Starting at your chosen charge, it repeatedly asks "can I reach the destination while keeping my
   buffer?" If not, it picks the next stop:
   - **Standard** candidates arrive with at least your buffer left.
   - **Flex** candidates dip into the buffer (down to half of it) but only if they are DC fast (>50 kW).
   - **Unhealthy** candidates are stations flagged as down. They are used only if nothing else is reachable.
   - From the candidates, the farthest 30 km window is considered and scored: `power × 10 + route position`, with a
     +15 bonus for working stations and −50 for flagged ones. So it prefers fast, working stations as far along as it
     safely can.
4. **Backup.** For each stop, the closest *different, working* station along the route is recorded as its backup
   (only if within 60 km).
5. **Charging.** After a stop the car is assumed charged to 85% (DC charging slows sharply above that). The **last**
   stop only charges what is needed to finish with your buffer plus 3%, which saves time.
6. **Time.** Charging time = 5 min overhead + energy ÷ effective power, where effective power is
   `min(charger kW, 150) × 0.72` (an average over a 10-85% session).
7. **Output.** Stops with arrival/departure charge, backups, charge minutes, the factor totals, and the per-point
   arrays used for the battery graph. If the trip cannot be completed, it still returns the stops it found plus a
   message saying where it ran out.

---

## The charger data pipeline

Open Charge Map (OCM) is a community database of chargers. Marga keeps a local copy and a daily health overlay.

- **`every_charger_india.json`**: the national registry, `{last_updated, count, chargers: [...]}`. Written by
  `refresh_chargers.py` (an atomic write, so a reader never sees half a file).
- **`charger_health.json`**: per-charger results from `check_charger_health.py`: working or not, a 0-100
  confidence, the reasons, when it was checked, and a consecutive-failure streak.
- **`core/charger_registry.py`** loads both and merges the health result onto each charger.

**How "is this charger working?" is decided.** A physical plug cannot be tested remotely, so the check uses the best
signals OCM has:

- the operator's `StatusType.IsOperational` flag (+20 confidence if true, −45 if explicitly false),
- whether the entry is a verified submission (+10, or −15 if not),
- driver check-ins in the last 30 days (+8 for each positive report, −20 for each negative one),
- any negative check-in in the last 14 days counts as a reported fault and marks it down.

Confidence starts at 50; a charger is "working" if confidence is at least 40, it is not explicitly non-operational,
and there is no recent fault report. A charger that disappears from OCM entirely is marked down. Chargers that have
never been checked show as "Not checked yet" and are treated as working.

**Automatic daily updates.** When the server starts it launches a background thread. Every 30 minutes it checks the
age of the two files; if a file is older than 24 hours **and** `OCM_API_KEY` is set, it refreshes the registry and/or
runs the health check. The running server notices the new files (by modification time) and reloads them without a
restart.

Chargers below 25 kW, and two-wheeler networks (Ather, Ola, Revolt, Bounce, Yulu, e:Swap, TVS, Honda), are filtered
out when loaded.

---

## Security and abuse protection

The site is public, and every plan request fans out to third-party services, so the server defends itself:

- **Rate limits** per client IP (per minute): 90 place searches, 20 route searches, 20 plans, 30 weather lookups.
  Exceeding them returns HTTP 429.
- **Strict input validation.** Vehicle numbers have sane min/max bounds (Pydantic); coordinates must be inside India;
  route ids expire after an hour and only the server knows the geometry behind them.
- **No injected HTML.** Place names, station names and other text from the server or third parties is always
  inserted with `textContent`, never `innerHTML` (the only `innerHTML` use is for the built-in icon shapes).
- **Content-Security-Policy** limits scripts to the site itself (plus one hashed inline redirect), connections to the
  same origin, images to the site and Esri's tile server. Also `X-Frame-Options: DENY`, `nosniff`, and a strict
  referrer policy.
- **No secrets in the repository.** API keys live in `.env`, which is git-ignored. (An older version of this project
  had a real key committed; that key must be revoked, and this version never stores keys in code.)
- **Third-party assets are self-hosted** (Leaflet, the font), so there is no third-party script on the page.

---

## File by file

### Server

**`server.py`** – the whole backend in one file.
- `RegistryCache`: loads the charger registry + health overlay, filters unusable chargers, and reloads
  automatically when either file changes on disk.
- `RouteStore`: in-memory store of route geometries (1-hour lifetime, max 300), so the browser never re-uploads them.
- `RateLimiter` / `throttle`: the per-IP limits above. Uses `X-Forwarded-For` when behind a proxy.
- `maintenance_loop` + `lifespan`: the daily refresh/health thread.
- `PrefixStrip`: an ASGI wrapper that treats `/marga/...` the same as `/...` and redirects `/marga` to `/marga/`,
  so the site works both at a domain root and behind Tailscale's `/marga` path.
- Endpoints: `GET /api/vehicles`, `GET /api/status`, `GET /api/geocode`, `GET /api/reverse` (for "use my
  location"), `POST /api/routes`, `POST /api/plan`, `GET /api/weather-now`.
- `_route_environment`: fetches elevation and weather in parallel and caches them on the route.
- `_corridor_chargers`: keeps chargers within 10 km of the route (bounding-box prefilter, then distance check).
- `_index_response`: serves `index.html` with a Content-Security-Policy whose hash is computed from the page.
- `asgi_app`: the app wrapped in `PrefixStrip`; this is what uvicorn runs.

### Core logic (`core/`)

**`core/physics.py`** – the force-balance energy model described above: `SegmentConditions`,
`segment_energy_parts` (six-way split), `segment_energy_wh` (the total), `air_density`, `hvac_load_kw`, plus the
constants (regen efficiency and cap, driving-style multipliers).

**`core/simulator.py`** – walks the route in energy space and plans stops: `_build_energy_profile`,
`_project_chargers`, the stop-selection loop in `simulate_trip`, `_charge_minutes`, `_finalize_stops`.

**`core/elevation.py`** – gets the elevation profile of a route from Open-Meteo's free elevation API. It samples up
to 100 points along the route in a single request and interpolates between them so every route point has an
elevation.

**`core/geo.py`** – shared geometry: `haversine_km` (distance), `bearing_deg` (compass direction, used for wind), and
`nearest_route_point` (vectorised closest-point search).

**`core/charger_registry.py`** – loads `every_charger_india.json` (old flat-list and new wrapped formats) and
`charger_health.json`, and merges the health fields onto each charger.

**`core/ai_optimizer.py`** – optional. If `GEMINI_API_KEY` is set, asks Gemini to phrase a one- or two-sentence
explanation of why the range differs from the baseline; otherwise builds the sentence itself. It never changes the
numbers, so results are identical with or without a key.

### External data (`api/`)

**`api/routing.py`** – asks OSRM for up to three driving routes (with a second mirror as failover). If both fail it
returns a straight-line estimate flagged `fallback`, which the UI labels as such.

**`api/weather.py`** – current weather from OpenWeatherMap (if `OPENWEATHER_API_KEY` is set) or Open-Meteo
(keyless fallback): temperature, wind speed and the wind's *from* direction. `sample_weather_along_route` takes six
evenly spaced readings along the route (the server runs this at the same time as the elevation lookup), and each
route segment uses the nearest reading.
`headwind_component_kmh` is the wind-versus-bearing projection.

### Data (`data/`)

**`data/vehicles.py`** – the database of ~50 EVs sold in India. Each has battery size, a flat Wh/km baseline, the
claimed range and test standard, plus the physical parameters: **drag coefficient (Cd), frontal area, mass, motor +
inverter efficiency (MRE)** and rolling-resistance coefficient. Cd is the manufacturer's figure where one is
published; frontal area, mass, MRE and rolling resistance are class-based engineering estimates, which is why the
app lets you edit all of them.

### Charger scripts (repository root)

**`refresh_chargers.py`** – `run_refresh()` downloads the full Indian charger list from Open Charge Map and writes
`every_charger_india.json`. Needs `OCM_API_KEY`. Can also be run by hand.

**`check_charger_health.py`** – `run_health_check()` re-queries OCM in batches of 100 for status and recent
check-ins and writes `charger_health.json` using the rules above. Needs `OCM_API_KEY`.

**`every_charger_india.json`** – the charger registry (ships with an older snapshot; replaced automatically once an
OCM key is configured).

**`charger_health.json`** – generated by the health check; does not exist until the first run.

### Website (`web/`)

**`web/index.html`** – the page skeleton: the trip form (places, car, driving), the results area, the legend, and a
tiny inline script that adds a trailing slash to the URL so relative links always work.

**`web/css/style.css`** – the whole design: colour tokens for light and dark themes (warm paper background, cobalt
route, vermilion stops, marigold backups, no gradients), the floating panel, cards, timeline, chart styles, map
marker styles, and the mobile bottom-sheet layout (at 860 px and below).

**`web/js/app.js`** – application logic: state, place search with keyboard support, the searchable car picker, the
fine-tune sliders, driving controls, the plan flow, and rendering of the range card, itinerary, energy breakdown and
conditions. Remembers your last car and settings in `localStorage`.

**`web/js/api.js`** – tiny `fetch` wrapper with friendly error messages. Uses relative URLs, so it works at `/` and
at `/marga/`.

**`web/js/map.js`** – the Leaflet map: Esri gray-canvas base tiles (light and dark), route drawing (with a white
outline and clickable alternatives), numbered stop markers, diamond backup markers, charger dots (hollow grey for
flagged-down), and the moving dot that follows your cursor on the chart.

**`web/js/chart.js`** – the battery-and-terrain SVG chart drawn by hand (no chart library): charge line, elevation
area, your buffer line, numbered stop markers, and a hover/touch tooltip.

**`web/js/util.js`** – a safe DOM builder (`el`), inline icons, number/time formatting, `localStorage` helpers,
debounce and haversine.

**`web/vendor/leaflet/`** – Leaflet 1.9.4 (BSD-2-Clause), self-hosted.
**`web/fonts/bricolage-latin.woff2`** – the Bricolage Grotesque typeface (SIL Open Font License), self-hosted.
**`web/favicon.svg`** – the logo mark.

### Deployment (`deploy/`)

**`deploy/install.sh`** – creates the virtual environment, installs dependencies, creates `.env` from the example,
writes a macOS LaunchAgent (start at login, restart on crash) and starts it.

**`deploy/com.marga.server.plist`** – the LaunchAgent template that `install.sh` fills in.

**`deploy/publish.sh`** – adds a `/marga` route to Tailscale. `--public` uses Funnel (internet); without it, the
site stays private to your tailnet. It deliberately refuses to run the private mode if that would switch off Funnel
for your other sites (a Tailscale behaviour that is easy to trip over).

### Tests and config

**`tests/test_core.py`** – 10 unit tests: energy parts sum to the total; headwind costs and tailwind helps; climbs
cost and descents return energy; driving-style ordering; hot and cold cost more than mild; stops have backups and
charge times; flagged-down stations are avoided; factor totals match trip energy; a failed trip still returns
complete stops; a lower start charge needs at least as many stops.

**`requirements.txt`** – Python dependencies (FastAPI, uvicorn, requests, polyline, NumPy, python-dotenv,
google-genai).

**`.env.example`** – template for the API keys. Copy to `.env`.

**`.gitignore`** – keeps `.env`, `.venv/`, caches and temp files out of git.

**`LICENSE`** – the project licence.

---

## Running, testing and deploying

```bash
# one-time setup and start-at-login service
deploy/install.sh

# put it on the internet (Tailscale Funnel), adding only /marga
deploy/publish.sh --public

# run tests
.venv/bin/python -m unittest discover -s tests -v

# restart after editing code
launchctl kickstart -k gui/$(id -u)/com.marga.server

# logs
tail -f ~/Library/Logs/marga.log
```

To run manually for development: `.venv/bin/python -m uvicorn server:asgi_app --port 8090 --reload`.

Add your keys to `.env` (`OCM_API_KEY` is the important one: without it the charger data does not update itself).

---

## Known limits

- **Estimates, not guarantees.** The physics model is far closer to reality than a brochure figure, but it uses one
  average speed for the whole route, class-based estimates for some vehicle parameters, and a single weather reading
  per stretch. Always keep a margin.
- **Charger health is inferred.** It comes from operator flags and driver check-ins, not a live connection to the
  charger. "Not checked yet" and "Reported working" do not guarantee a free, working plug.
- **India only** (place search and routing are limited to India).
- **Uptime.** The site runs on one Mac, so it is only available while that Mac is on and online.
- **Free services.** Routing (OSRM), maps (Esri) and geocoding (ArcGIS) are free public services without an SLA.
