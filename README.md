# AEGIS — Emergency Guidance & Intelligent Safety

AEGIS is an educational Streamlit prototype for comparing routes, exploring
graph-search algorithms, and viewing weather and disaster-alert information.
It is advisory only and is not a certified emergency-navigation or life-safety
system.

## Requirements and startup

The project is tested with Python 3.14 and the pinned packages in
`requirements.txt`. On Windows, from the project root:

```powershell
.\.venv314\Scripts\python.exe -m pip install -r requirements.txt
.\.venv314\Scripts\python.exe -m streamlit run app.py
```

Use `http://localhost:8501` in a browser. Browser geolocation requires an
explicit click on **Use My Current Location** and user permission. Browsers
allow geolocation on localhost or a secure HTTPS origin; manual address entry
is available if the browser cannot provide a location.

## Features and architecture

- `app.py` — Streamlit dashboard, route selection, maps, emergency panels, and
  user-facing status and safety notices.
- `real_routing.py` and `multi_route_planner.py` — OpenStreetMap Nominatim
  geocoding, OSRM road routes and alternatives, route comparisons, and
  user-entered hazard proximity checks.
- `road_network.py`, `route_algorithms.py`, and `evacuation_simulator.py` —
  separate academic graph with BFS, Uniform Cost Search, A*, and simulated
  blocked-road rerouting.
- `weather_service.py` and `emergency_monitor.py` — Open-Meteo weather and
  river-discharge forecasts, displayed as forecast information or screening
  indicators rather than official warnings.
- `disaster_alert_service.py` — location-based records from the NDMA SACHET
  portal, including report freshness, validity, and available source geometry.
- `offline_data.py` and `data/aegis_offline.sqlite3` — imported local GeoJSON
  roads and places, locally calculated routes within available coverage, and
  saved successful reports.
- `location_component/index.html` — browser permission button and location
  result display.
- `styles.css` — AEGIS royal-blue (`#014baa`) and light-cream (`#f8f3f0`)
  interface theme.

## Data sources and configuration

The current integrations use public OpenStreetMap Nominatim and OSRM services,
Open-Meteo APIs, and the NDMA SACHET portal. These services can be unavailable,
rate-limited, delayed, or changed. Route calculations, forecasts, and alert
records are shown with their available source and freshness context; the
absence of a report does not mean an area is safe.

No API key is required by the currently integrated providers. Set
`NOMINATIM_CONTACT` to a real project contact when making Nominatim requests,
in accordance with its usage policy. Do not put secrets in source code or
commit them to the repository.

To use offline real-world routing, import a local GeoJSON dataset containing
road lines and, optionally, named places or shelters. Offline routes depend on
the imported data and its geographic coverage. Live geocoding, new weather
forecasts, and new disaster alerts require network access; saved reports may be
available offline but can be stale.

## Tests and validation

Run these commands from the project root:

```powershell
.\.venv314\Scripts\python.exe -m compileall -q .
.\.venv314\Scripts\python.exe -m unittest discover -s tests -v
.\.venv314\Scripts\python.exe -m streamlit run app.py
```

The unit tests mock external service responses and do not require live API
access. The Streamlit startup and interactive browser controls should also be
checked separately.

## Limitations and safety

- OSRM/Nominatim availability and service policies apply; no live traffic,
  verified closures, or guaranteed route availability is provided.
- User-entered hazard locations and available alert geometry are not a
  comprehensive hazard map. A route without a reported intersection is not
  thereby verified safe.
- SACHET coverage is location-based and may not cover an entire route. Its
  public endpoint is not a documented stability guarantee.
- Open-Meteo forecasts and river-discharge estimates do not constitute
  official warnings or validated local flood thresholds.
- Imported offline road data can be incomplete or outdated. Cached forecasts
  and reports may be stale or unavailable.
- The simulated network uses academic coordinates and synthetic graph weights;
  its results are demonstrations, not real-world navigation.
- Always follow current official emergency instructions and local authorities.
  AEGIS does not guarantee safety or predict every emergency.
