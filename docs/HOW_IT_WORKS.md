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

Marga exists in two forms that compute **identical** numbers from the **same formulas**, because one was ported
directly from the other and both are checked against each other in testing:

- **The live site** (`https://saiaarjay09.github.io/Marga/`) is a plain static site on GitHub Pages. There is no
  server at all: `web/js/*.js` does the routing, physics, charger matching and trip simulation right in your
  browser, calling the same free, CORS-enabled public services a backend would have called on your behalf. This is
  what makes it "always on" — nobody's computer needs to be running for the site to work.
- **An optional self-hosted copy** (`server.py` + `core/`/`api/` in Python) does the exact same computation
  server-side, for anyone who'd rather run their own backend (local development, a LAN deployment, or publishing a
  copy over Tailscale as this project originally did). It serves the identical `web/` frontend, so from the
  browser's point of view the two are indistinguishable except for where the work happens.

```
 Browser (web/)                                            Outside world
┌─────────────────────────────┐   fetch()
│ index.html + css + js        │ ─────────────────────────► ArcGIS geocoder (place search)
│  · form, car picker          │ ─────────────────────────► OSRM (road routes)
│  · Leaflet map                │ ─────────────────────────► Open-Meteo (elevation + weather)
│  · battery chart              │
│  · js/api.js orchestrates:    │
│      js/routing.js            │          (on GitHub Pages: everything above runs IN the browser)
│      js/elevation.js          │
│      js/weather.js            │
│      js/chargers.js ──fetch──►│ data/every_charger_india.json, data/charger_health.json  (static files)
│      js/simulator.js          │
│        └─ js/physics.js       │
│      js/explain.js            │
└───────────────────────────────┘

 Optional self-hosted backend (server.py, FastAPI) -- same job, done in Python instead of in the browser:
┌──────────────────────────────┐
│ /api/geocode/routes/plan  ────┼──► the same ArcGIS / OSRM / Open-Meteo calls, from Python
│  core/simulator + physics     │
│  core/charger_registry        │
│  background thread, daily: ───┼──► Open Charge Map (refresh_chargers.py, check_charger_health.py)
└────────────────────────────────┘

 GitHub Actions (keeps the GitHub Pages copy's charger data fresh without any server):
   refresh-chargers.yml (daily cron)  ──► Open Charge Map ──► commits web/data/*.json
   pages.yml (on every push to web/)  ──► publishes web/ as the live site
```

There is no database anywhere: the charger list and its health results are two JSON files under `web/data/`, read
directly by the browser (or, in self-hosted mode, by `server.py`).

---

## What happens when you press "Plan my trip"

These steps are the same on both the live site and a self-hosted copy; only *where* each step runs differs
(browser-side JS vs. server-side Python calling the identical logic).

1. **Places.** If you typed a place but did not pick a suggestion, `api.geocode()` queries ArcGIS and takes the top
   match, limited to India.
2. **Routes.** `api.routes()` asks OSRM for up to three driving route alternatives, keeps each route's full geometry
   (in the browser's memory on the static site; in the server's memory for a self-hosted one) under a generated id,
   and returns a thinned copy to draw.
3. **Plan.** `api.plan()` is given the route id plus your car, driving style, start charge and buffer. It then:
   1. Fetches the **elevation profile** and **weather along the route** in parallel (cached against the route so
      switching settings and replanning doesn't re-fetch them).
   2. Finds every **fast charger within 10 km of the route**, from the charger registry with its health overlay.
   3. Runs the **simulator**, which walks the route in small steps computing energy use, and decides where to stop.
   4. Builds the result: range comparison, itinerary with backups, battery-level graph, a breakdown of where the
      energy went, weather summary, and registry freshness.
4. **Render.** `app.js`/`map.js`/`chart.js` draw the route, stops, backups and nearby chargers on the map, and fill
   the panel -- these three files are identical in both deployments; they don't know or care whether `api.js` did
   the work locally or asked a server.

Typical time is 3-6 seconds, dominated by the weather and elevation lookups.

---

## The physics model

File: `core/physics.py` (Python) / `web/js/physics.js` (the line-for-line JS port the live site actually runs).
Both are verified to produce identical numbers for the same inputs. For every short segment of the route it
balances the forces on the car:

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

File: `core/simulator.py` / `web/js/simulator.js` (same two-implementation setup as the physics model, checked
against each other with a shared test route). It works in **energy (Wh)**, not kilometres, because energy per km
changes with hills and wind.

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

Open Charge Map (OCM) is a community database of chargers. Marga keeps a local copy and a daily health overlay,
both under `web/data/` so the same two files are what the live GitHub Pages site fetches directly *and* what a
self-hosted `server.py` reads from disk.

- **`web/data/every_charger_india.json`**: the national registry, `{last_updated, count, chargers: [...]}`. Written
  by `refresh_chargers.py` (an atomic write, so a reader never sees half a file).
- **`web/data/charger_health.json`**: per-charger results from `check_charger_health.py`: working or not, a 0-100
  confidence, the reasons, when it was checked, and a consecutive-failure streak.
- **`core/charger_registry.py`** (Python) / **`web/js/chargers.js`** (browser) each load both files and merge the
  health result onto every charger, filtering out sub-25kW and two-wheeler-network entries.

**How "is this charger working?" is decided.** A physical plug cannot be tested remotely, so the check uses the best
signals OCM has:

- the operator's `StatusType.IsOperational` flag (+20 confidence if true, −45 if explicitly false),
- whether the entry is a verified submission (+10, or −15 if not),
- driver check-ins in the last 30 days (+8 for each positive report, −20 for each negative one),
- any negative check-in in the last 14 days counts as a reported fault and marks it down.

Confidence starts at 50; a charger is "working" if confidence is at least 40, it is not explicitly non-operational,
and there is no recent fault report. A charger that disappears from OCM entirely is marked down. Chargers that have
never been checked show as "Not checked yet" and are treated as working.

**Automatic daily updates.** For the live GitHub Pages site, `.github/workflows/refresh-chargers.yml` runs on a
daily cron, calls `refresh_chargers.py` and `check_charger_health.py` the same way a self-hosted copy would, and
commits the two files if they changed; `.github/workflows/pages.yml` then redeploys automatically because that
commit touched `web/`. No server or key-holding backend is needed for this to keep working.

A self-hosted `server.py` instead runs its own background thread: every 30 minutes it checks the age of the two
files, and if one is older than 24 hours **and** `OCM_API_KEY` is set in its `.env`, it runs the same refresh/health
check itself and reloads the files (by modification time) without a restart.

Chargers below 25 kW, and two-wheeler networks (Ather, Ola, Revolt, Bounce, Yulu, e:Swap, TVS, Honda), are filtered
out when loaded.

---

## Security and abuse protection

On the **live GitHub Pages site** there is no server to attack or overload: each visitor's browser calls ArcGIS,
OSRM and Open-Meteo directly over their own connection, so there's nothing for Marga itself to rate-limit (fair-use
limits are between each visitor's browser and that public service, not something a static site can or needs to
police). What the static deployment still does:

- **No injected HTML.** Place names, station names and other text from third parties is always inserted with
  `textContent`, never `innerHTML` (the only `innerHTML` use is for the handful of built-in, hardcoded icon shapes).
- **Content-Security-Policy**, via a `<meta>` tag (GitHub Pages can't set custom HTTP headers): scripts limited to
  the site itself, connections limited to the same origin plus the specific APIs called (ArcGIS, OSRM, Open-Meteo),
  images to the site and Esri's tile server.
- **No secrets anywhere in the client.** The static build never holds an OpenWeatherMap or Gemini key, which is
  exactly why it doesn't use them -- there's nowhere safe in browser-shipped code to keep a secret.
- **Third-party assets are self-hosted** (Leaflet, the font), so there is no third-party `<script>` on the page.
- **India-only bounds and input validation** are enforced in `web/js/api.js` the same way the server used to.

The **optional self-hosted `server.py`** adds the protections a real backend needs, since it proxies third-party
calls through one IP for everyone who uses it:

- **Rate limits** per client IP (per minute): 90 place searches, 20 route searches, 20 plans, 30 weather lookups.
  Exceeding them returns HTTP 429.
- **Strict input validation** via Pydantic bounds on vehicle numbers and coordinates.
- The same Content-Security-Policy, delivered as a real HTTP header (plus `X-Frame-Options: DENY`, `nosniff`, and a
  strict referrer policy -- protections a `<meta>` tag can't fully express).
- **No secrets in the repository.** API keys live in `.env`, which is git-ignored. (An older version of this
  project had a real key committed; that key must be revoked, and this version never stores keys in code.)

---

## File by file

### Server

**`server.py`** – the whole backend in one file, used only by a self-hosted copy. Its `/api/*` endpoints aren't
called by the live GitHub Pages site (which has no server to call), but the exact same `web/` frontend works
against them unchanged if you run `server.py` -- `app.js` has no idea whether `api.js` did the work locally or
asked a server, so nothing here needed to change when the static build was added.
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

**`core/charger_registry.py`** – loads `web/data/every_charger_india.json` (old flat-list and current wrapped
formats both work) and `web/data/charger_health.json`, and merges the health fields onto each charger. Mirrored by
`web/js/chargers.js` for the browser.

**`core/ai_optimizer.py`** – optional, self-hosted only. If `GEMINI_API_KEY` is set, asks Gemini to phrase a one- or
two-sentence explanation of why the range differs from the baseline; otherwise builds the sentence itself. It never
changes the numbers, so results are identical with or without a key. The static build has nowhere safe to hold a
Gemini key, so `web/js/explain.js` always uses the deterministic fallback sentence -- the same text a self-hosted
copy without a key would show.

### External data (`api/`)

**`api/routing.py`** – asks OSRM for up to three driving routes (with a second mirror as failover). If both fail it
returns a straight-line estimate flagged `fallback`, which the UI labels as such.

**`api/weather.py`** – current weather from OpenWeatherMap (if `OPENWEATHER_API_KEY` is set) or Open-Meteo
(keyless fallback): temperature, wind speed and the wind's *from* direction. `sample_weather_along_route` takes six
evenly spaced readings along the route (the server runs this at the same time as the elevation lookup), and each
route segment uses the nearest reading. `headwind_component_kmh` is the wind-versus-bearing projection.
`web/js/weather.js` is the browser port -- it always uses Open-Meteo (no safe place to hold an OWM key client-side,
and no key was ever configured in production, so this changes nothing in practice).

### Data (`data/`)

**`data/vehicles.py`** – the database of ~50 EVs sold in India. Each has battery size, a flat Wh/km baseline, the
claimed range and test standard, plus the physical parameters: **drag coefficient (Cd), frontal area, mass, motor +
inverter efficiency (MRE)** and rolling-resistance coefficient. Cd is the manufacturer's figure where one is
published; frontal area, mass, MRE and rolling resistance are class-based engineering estimates, which is why the
app lets you edit all of them.

### Charger scripts (repository root)

**`refresh_chargers.py`** – `run_refresh()` downloads the full Indian charger list from Open Charge Map and writes
`web/data/every_charger_india.json`. Needs `OCM_API_KEY`. Run by the daily GitHub Actions workflow, by a
self-hosted server's background thread, or by hand.

**`check_charger_health.py`** – `run_health_check()` re-queries OCM in batches of 100 for status and recent
check-ins and writes `web/data/charger_health.json` using the rules above. Needs `OCM_API_KEY`.

**`web/data/every_charger_india.json`** – the charger registry, fetched directly by the browser on the live site
and read from disk by a self-hosted `server.py`.

**`web/data/charger_health.json`** – generated by the health check, read the same two ways.

### Website (`web/`)

This whole directory is the live site, published as-is to GitHub Pages; it's also what `server.py` serves for a
self-hosted copy. Every file here works identically in both deployments.

**`web/index.html`** – the page skeleton: the trip form (places, car, driving), the results area, the legend, a
Content-Security-Policy `<meta>` tag (for the no-server deployment), and a tiny external script
(`js/boot.js`) that adds a trailing slash to the URL so relative links always work.

**`web/css/style.css`** – the whole design: colour tokens for light and dark themes (warm paper background, cobalt
route, vermilion stops, marigold backups, no gradients), the floating panel, cards, timeline, chart styles, map
marker styles, and the mobile bottom-sheet layout (at 860 px and below).

**`web/js/app.js`** – application logic: state, place search with keyboard support, the searchable car picker, the
fine-tune sliders, driving controls, the plan flow, and rendering of the range card, itinerary, energy breakdown and
conditions. Remembers your last car and settings in `localStorage`. Unchanged by the move to a static build -- it
only ever talks to `api.js`, never to the network directly.

**`web/js/api.js`** – the orchestrator. Implements the exact same `vehicles() / status() / geocode() / reverse() /
routes() / plan()` functions server.py's endpoints used to provide, but does the work itself: calls
`routing.js`/`elevation.js`/`weather.js` for the route's environment, `chargers.js` for the registry, matches
chargers to the route corridor, and runs `simulator.js`. Keeps routes in an in-memory `Map` (capped at 20) instead
of re-fetching them.

**`web/js/physics.js`** – line-for-line JS port of `core/physics.py` (see "The physics model" above). Verified
against it with shared test cases.

**`web/js/simulator.js`** – line-for-line JS port of `core/simulator.py` (see "The trip simulator" above). Verified
against it on a real 600km route with synthetic chargers, including down-station avoidance and backups.

**`web/js/geo.js`** – port of `core/geo.py`: haversine distance, bearing, and a coarse-then-refine nearest-route-point
search (fast enough for a few hundred chargers against a multi-thousand-point route without needing NumPy).

**`web/js/elevation.js`** – port of `core/elevation.py`: samples the route every ~2km via Open-Meteo's elevation API
and interpolates between samples.

**`web/js/weather.js`** – port of `api/weather.py`, Open-Meteo only (see note above on why no OWM key).

**`web/js/routing.js`** – port of `api/routing.py`: calls OSRM (with the same mirror + straight-line fallback), and
includes a from-scratch polyline decoder (verified byte-for-byte against Python's `polyline` library) since there's
no npm dependency step for a static site to run.

**`web/js/chargers.js`** – port of `core/charger_registry.py`'s merge logic plus `server.py`'s `RegistryCache`
filtering, fetching the two JSON files as plain static assets and caching the merged result for the page's lifetime.

**`web/js/vehicles.js`** – the ~50-EV database, mechanically generated from `data/vehicles.py` (a small script
regex-extracts every `VehicleSpec(...)` call so the two can't drift out of sync by a typo).

**`web/js/explain.js`** – the deterministic range-explanation sentence, ported from `core/ai_optimizer.py`'s
non-Gemini fallback.

**`web/js/map.js`** – the Leaflet map: Esri gray-canvas base tiles (light and dark), route drawing (with a white
outline and clickable alternatives), numbered stop markers, diamond backup markers, charger dots (hollow grey for
flagged-down), and the moving dot that follows your cursor on the chart.

**`web/js/chart.js`** – the battery-and-terrain SVG chart drawn by hand (no chart library): charge line, elevation
area, your buffer line, numbered stop markers, and a hover/touch tooltip.

**`web/js/util.js`** – a safe DOM builder (`el`), inline icons, number/time formatting, `localStorage` helpers,
debounce and haversine.

**`web/js/boot.js`** – the trailing-slash redirect, as an external file so no inline-script CSP exception is needed.

**`web/data/every_charger_india.json`**, **`web/data/charger_health.json`** – see "The charger data pipeline" above.

**`web/vendor/leaflet/`** – Leaflet 1.9.4 (BSD-2-Clause), self-hosted.
**`web/fonts/bricolage-latin.woff2`** – the Bricolage Grotesque typeface (SIL Open Font License), self-hosted.
**`web/favicon.svg`** – the logo mark.

### Deployment

**`.github/workflows/pages.yml`** – publishes `web/` to GitHub Pages on every push that touches it (including the
daily data-refresh bot commit), using GitHub's standard Pages Actions (`configure-pages`, `upload-pages-artifact`,
`deploy-pages`). This is what makes the live site "always on": it's rebuilt automatically, nobody's machine needs
to be running.

**`.github/workflows/refresh-chargers.yml`** – daily cron (+ manual trigger) that runs `refresh_chargers.py` and
`check_charger_health.py` and commits the result if it changed, using an `OCM_API_KEY` repository secret. That
commit is what triggers `pages.yml` to redeploy with fresh data.

**`deploy/install.sh`** – (self-hosted only) creates the virtual environment, installs dependencies, creates `.env`
from the example, writes a macOS LaunchAgent (start at login, restart on crash) and starts it.

**`deploy/com.marga.server.plist`** – the LaunchAgent template that `install.sh` fills in.

**`deploy/publish.sh`** – (self-hosted only) adds a `/marga` route to Tailscale. `--public` uses Funnel (internet);
without it, the site stays private to your tailnet. It deliberately refuses to run the private mode if that would
switch off Funnel for your other sites (a Tailscale behaviour that is easy to trip over).

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

**The live site** deploys itself: push to `main` and `.github/workflows/pages.yml` republishes `web/`. To run your
own fork, fork the repo and set **Settings → Pages → Source → GitHub Actions**, then add an `OCM_API_KEY`
repository secret so `refresh-chargers.yml` can keep your copy's charger data current. `workflow_dispatch` is
enabled on both workflows if you want to trigger a run manually instead of waiting for the next push or the daily
cron.

To preview `web/` locally with zero backend (exactly what GitHub Pages serves): `cd web && python3 -m http.server
8092`, then open `http://127.0.0.1:8092`.

**A self-hosted copy**, if you want one:

```bash
# one-time setup and start-at-login service
deploy/install.sh

# put it on the internet (Tailscale Funnel), adding only /marga
deploy/publish.sh --public

# run tests (covers the Python physics/simulator implementation)
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
- **Free services.** Routing (OSRM), maps (Esri) and geocoding (ArcGIS) are free public services without an SLA;
  the live site depends on their continued availability since it calls them directly, same as a backend would.
- **A self-hosted copy's uptime** is only as good as the machine running it; the live GitHub Pages site doesn't
  have this limit, since there's no server to be offline.
