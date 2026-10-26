"""
app.py
AI-Based Emergency Evacuation Route Planner - Multi-Route Planning Dashboard.

Features:
1. Real-World Multi-Route Planning:
   - Geocoding via OpenStreetMap Nominatim
   - Shortest-distance primary route
   - Multiple distinct alternative dispersal corridors
   - Hazard-avoidance rerouting with simulated hazard zones
   - Nearest reachable shelter comparison (user-provided, unverified)
   - Interactive route highlighting, distinct map colors, and overlap-friendly rendering
2. Academic Simulated Mode:
   - 6-intersection graph (A-F) with NetworkX K-shortest simple paths
   - BFS, UCS, and A* algorithm comparisons
   - Dynamic road closures and unreachable network partition alerts
"""

from datetime import datetime, timezone
import hashlib
import html
import json
import math
import os
from typing import Optional
import folium
import streamlit.components.v1 as components
from multi_route_planner import (
    check_route_hazard_proximity,
    generate_real_world_multi_routes,
    generate_simulated_k_shortest_paths,
)
from real_routing import geocode_location, reverse_geocode_location
from emergency_monitor import (
    DEFAULT_RAINFALL_SCREENING_THRESHOLD_MM,
    assess_emergency_conditions,
    emergency_alert_id,
    get_flood_forecast,
    last_successful_report,
    reconcile_active_alerts,
)
from disaster_alert_service import (
    SACHET_ALERT_PORTAL_URL,
    alert_covers_point,
    alert_time_state,
    classify_alert_report,
    clear_alert_cache,
    fetch_location_alerts,
    report_is_stale,
    route_intersects_alert,
)
from road_network import create_road_network
from route_algorithms import (
    a_star_search,
    breadth_first_search,
    uniform_cost_search,
)
from offline_data import (
    database_summary,
    generate_local_routes,
    import_geojson,
    load_cached_report,
    local_road_segments,
    render_local_map_svg,
    save_cached_report,
    search_local_places,
)
import streamlit as st
from streamlit_folium import st_folium
from weather_service import get_weather

# -----------------------------------------------------------------------------
# 1. Page Configuration & Setup
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="AEGIS | Emergency Guidance & Intelligent Safety",
    page_icon="🚨",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Simulated academic coordinates for Mode 2
SIMULATED_COORDINATES = {
    "A": (37.7780, -122.4350),  # Starting Point (West)
    "B": (37.7920, -122.4200),  # Northern Intersection
    "C": (37.7660, -122.4250),  # Southern Intersection
    "D": (37.7880, -122.4000),  # North-Eastern Intersection
    "E": (37.7640, -122.4050),  # South-Eastern Intersection
    "F": (37.7760, -122.3880),  # Safe Destination (East)
}
SIMULATED_MAP_CENTER = (37.7770, -122.4115)
_LOCATION_COMPONENT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "location_component",
)
_browser_location_component = components.declare_component(
    "browser_location_picker",
    path=_LOCATION_COMPONENT_PATH,
)


# -----------------------------------------------------------------------------
# 2. Caching Wrappers for API Efficiency
# -----------------------------------------------------------------------------
def cached_geocode_address(query: str):
    """Geocode only when requested; real_routing caches successful results."""
    return geocode_location(query)


@st.cache_data(show_spinner=False, ttl=3600)
def cached_reverse_geocode(lat: float, lon: float):
    """Caches reverse-geocoding results for a detected coordinate pair."""
    return reverse_geocode_location(lat, lon)


@st.cache_data(show_spinner=False, ttl=900)
def cached_weather_forecast(lat: float, lon: float):
    """Caches Open-Meteo forecast responses for 15 minutes per coordinate pair."""
    return get_weather(lat, lon)


@st.cache_data(show_spinner=False, ttl=900)
def cached_flood_forecast(lat: float, lon: float):
    """Caches Open-Meteo Flood API responses for 15 minutes per coordinate pair."""
    return get_flood_forecast(lat, lon)


def refresh_forecast_reports(targets: list, force_refresh: bool = False) -> dict:
    """Fetch missing selected-location forecasts and persist reports for both panels."""
    if force_refresh:
        cached_weather_forecast.clear()
        cached_flood_forecast.clear()

    reports = st.session_state.get("emergency_monitor_reports", {})
    if not isinstance(reports, dict):
        reports = {}
    weather_reports = st.session_state.get("weather_reports", {})
    if not isinstance(weather_reports, dict):
        weather_reports = {}

    for _, latitude, longitude, location_name in targets:
        coordinate_key = f"{latitude:.6f},{longitude:.6f}"
        previous_entry = reports.get(coordinate_key, {})
        if not isinstance(previous_entry, dict):
            previous_entry = {}

        weather_report = previous_entry.get("weather")
        if force_refresh or not isinstance(weather_report, dict):
            weather_report = cached_weather_forecast(latitude, longitude)
        if isinstance(weather_report, dict) and weather_report.get("success"):
            save_cached_report("weather", latitude, longitude, weather_report)
        if isinstance(weather_report, dict):
            weather_reports[coordinate_key] = {
                "location_name": location_name,
                "report": weather_report,
            }

        flood_report = previous_entry.get("flood")
        if force_refresh or not isinstance(flood_report, dict):
            flood_report = cached_flood_forecast(latitude, longitude)
        if isinstance(flood_report, dict) and flood_report.get("success"):
            save_cached_report("flood", latitude, longitude, flood_report)

        reports[coordinate_key] = {
            "location_name": location_name,
            "weather": weather_report,
            "flood": flood_report,
            "weather_last_success": last_successful_report(
                weather_report,
                previous_entry.get("weather_last_success"),
            ),
            "flood_last_success": last_successful_report(
                flood_report,
                previous_entry.get("flood_last_success"),
            ),
        }

    st.session_state["emergency_monitor_reports"] = reports
    st.session_state["weather_reports"] = weather_reports
    return reports


def refresh_official_alert_reports(
    targets: list,
    force_refresh: bool = False,
) -> dict:
    """Fetch and persist SACHET reports for selected real-world locations."""
    if force_refresh:
        clear_alert_cache()

    reports = st.session_state.get("official_alert_reports", {})
    if not isinstance(reports, dict):
        reports = {}

    unique_targets = {}
    for label, latitude, longitude, location_name in targets:
        coordinate_key = f"{float(latitude):.5f},{float(longitude):.5f}"
        unique_targets.setdefault(
            coordinate_key,
            (label, float(latitude), float(longitude), location_name),
        )

    for coordinate_key, target in unique_targets.items():
        result = fetch_location_alerts(target[1], target[2])
        previous = reports.get(coordinate_key, {})
        if not isinstance(previous, dict):
            previous = {}
        if result.get("success"):
            save_cached_report(
                "official_alerts",
                target[1],
                target[2],
                result,
            )
            reports[coordinate_key] = {
                "last_success": result,
                "last_error": None,
                "last_attempt_utc": result.get("retrieved_at_utc"),
            }
        else:
            reports[coordinate_key] = {
                "last_success": previous.get("last_success"),
                "last_error": result.get(
                    "error",
                    "Official alert source unavailable.",
                ),
                "last_attempt_utc": result.get("checked_at_utc"),
            }

    st.session_state["official_alert_reports"] = reports
    return reports


@st.cache_data(show_spinner=False, ttl=1800)
def cached_multi_routes(
    s_lat: float,
    s_lon: float,
    d_lat: float,
    d_lon: float,
    hazard_data_tuple: tuple,
    shelter_data_tuple: tuple,
):
    """Caches multi-route calculations for given coordinates and parameters."""
    hazard_info = None
    if hazard_data_tuple and hazard_data_tuple[0] is not None:
        hazard_info = {
            "lat": hazard_data_tuple[0],
            "lon": hazard_data_tuple[1],
            "radius_km": hazard_data_tuple[2],
            "description": hazard_data_tuple[3],
        }

    candidate_shelters = None
    if shelter_data_tuple and shelter_data_tuple[0] is not None:
        candidate_shelters = [
            {
                "name": shelter_data_tuple[0],
                "lat": shelter_data_tuple[1],
                "lon": shelter_data_tuple[2],
            }
        ]

    return generate_real_world_multi_routes(
        start_lat=s_lat,
        start_lon=s_lon,
        dest_lat=d_lat,
        dest_lon=d_lon,
        hazard_info=hazard_info,
        candidate_shelters=candidate_shelters,
    )


# -----------------------------------------------------------------------------
# 3. Map Building Functions
# -----------------------------------------------------------------------------
def build_multi_route_map(
    start_lat: float,
    start_lon: float,
    start_name: str,
    dest_lat: float,
    dest_lon: float,
    dest_name: str,
    routes_dict: dict,
    highlight_route_id: str = "all",
    hazard_info: dict = None,
    shelter_info: dict = None,
    start_accuracy_m: float = None,
    official_alerts: list = None,
):
    """
    Renders all candidate evacuation routes on an OpenStreetMap Folium canvas.
    Applies prominent styling to the highlighted route while keeping other routes readable.
    """
    center_lat = (start_lat + dest_lat) / 2.0
    center_lon = (start_lon + dest_lon) / 2.0

    m = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=14,
        tiles="OpenStreetMap",
        attr='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
        control_scale=True,
    )

    all_lats, all_lons = [start_lat, dest_lat], [start_lon, dest_lon]

    # 1. Render Routes
    # Order rendering so the highlighted route is drawn on top
    route_keys = list(routes_dict.keys())
    if highlight_route_id in route_keys:
        route_keys.remove(highlight_route_id)
        route_keys.append(highlight_route_id)  # Draw last to be topmost

    for r_key in route_keys:
        r = routes_dict[r_key]
        coords = r.get("route_coords", [])
        if not coords:
            continue

        is_selected = (highlight_route_id == "all") or (highlight_route_id == r_key)

        # Visual styling: selected route is thick and vivid; non-selected routes are thinner & semi-transparent
        if highlight_route_id == "all":
            line_weight = 6
            line_opacity = 0.8
        elif is_selected:
            line_weight = 8
            line_opacity = 0.95
        else:
            line_weight = 4
            line_opacity = 0.45

        tooltip_text = (
            f"<b>{r['name']}</b><br/>"
            f"Distance: {r['distance_km']} km | Est. Time: {r['duration_mins']} min<br/>"
            f"<i>{r['hazard_status'] if hazard_info else 'No user-provided hazard data; not evaluated'}</i>"
        )
        intersecting_alerts = [
            alert
            for alert in (official_alerts or [])
            if route_intersects_alert(alert, coords) is True
        ]
        if intersecting_alerts:
            tooltip_text += (
                "<br/><b>Intersects a returned official alert area:</b> "
                + html.escape(", ".join(
                    str(alert.get("hazard_type", "Official alert"))
                    for alert in intersecting_alerts
                ))
            )

        folium.PolyLine(
            locations=coords,
            color=r["color"],
            weight=line_weight,
            opacity=line_opacity,
            tooltip=folium.Tooltip(tooltip_text),
        ).add_to(m)

        for pt in coords:
            all_lats.append(pt[0])
            all_lons.append(pt[1])

    # Official alert geometry is shown only when supplied by the issuing portal.
    alert_features = []
    for alert in official_alerts or []:
        geometry = alert.get("geometry")
        if not isinstance(geometry, dict):
            continue
        alert_features.append({
            "type": "Feature",
            "geometry": geometry,
            "properties": {
                "Hazard": html.escape(str(alert.get("hazard_type", "Official alert"))),
                "Severity": html.escape(str(alert.get("severity", "Not supplied"))),
                "Issuer": html.escape(str(alert.get("issuing_authority", "Not supplied"))),
                "Affected area": html.escape(str(alert.get("affected_region", "Not supplied"))),
            },
        })
        coordinates = geometry.get("coordinates", [])
        stack = [coordinates]
        while stack:
            value = stack.pop()
            if (
                isinstance(value, (list, tuple))
                and len(value) >= 2
                and all(isinstance(component, (int, float)) for component in value[:2])
            ):
                all_lons.append(float(value[0]))
                all_lats.append(float(value[1]))
            elif isinstance(value, (list, tuple)):
                stack.extend(value)
    if alert_features:
        folium.GeoJson(
            {"type": "FeatureCollection", "features": alert_features},
            name="Official alert areas (SACHET)",
            style_function=lambda feature: {
                "color": "#a61b1b",
                "weight": 2,
                "fillColor": "#e53e3e",
                "fillOpacity": 0.16 if feature.get("geometry") else 0,
            },
            tooltip=folium.GeoJsonTooltip(
                fields=["Hazard", "Severity", "Issuer", "Affected area"],
                aliases=["Hazard", "Source severity", "Issuing authority", "Affected area"],
                sticky=True,
            ),
        ).add_to(m)

    # 2. Simulated Hazard Zone & Marker
    if hazard_info and hazard_info.get("lat") and hazard_info.get("lon"):
        h_lat, h_lon = hazard_info["lat"], hazard_info["lon"]
        h_rad_m = hazard_info.get("radius_km", 0.5) * 1000.0
        h_desc = hazard_info.get("description", "Simulated Hazard Zone")

        # Translucent danger buffer circle
        folium.Circle(
            location=[h_lat, h_lon],
            radius=h_rad_m,
            color="#e53e3e",
            fill=True,
            fill_color="#e53e3e",
            fill_opacity=0.25,
            weight=2,
            dash_array="6, 6",
            tooltip=f"⚠️ {h_desc} (Buffer: {h_rad_m:.0f}m)",
        ).add_to(m)

        # Hazard Center Pin
        folium.Marker(
            location=[h_lat, h_lon],
            popup=folium.Popup(
                f"<b>⚠️ Simulated Hazard Zone</b><br/>{h_desc}<br/><i>Status: Unverified / Simulated Input</i>",
                max_width=300,
            ),
            tooltip=f"Hazard Point: {h_desc}",
            icon=folium.Icon(color="orange", icon="warning-sign", prefix="glyphicon"),
        ).add_to(m)
        all_lats.append(h_lat)
        all_lons.append(h_lon)

    # 3. Candidate Shelter Marker (User-provided, unverified)
    if shelter_info and shelter_info.get("lat") and shelter_info.get("lon"):
        s_lat, s_lon = shelter_info["lat"], shelter_info["lon"]
        s_name = shelter_info.get("name", "Candidate Shelter")
        folium.Marker(
            location=[s_lat, s_lon],
            popup=folium.Popup(
                f"<b>🛡️ Candidate Evacuation Shelter</b><br/>{s_name}<br/>"
                f"<i>Notice: User-provided and unverified. Confirm with civil defense authorities.</i>",
                max_width=320,
            ),
            tooltip=f"Candidate Shelter: {s_name}",
            icon=folium.Icon(color="cadetblue", icon="home", prefix="glyphicon"),
        ).add_to(m)
        all_lats.append(s_lat)
        all_lons.append(s_lon)

    # 4. Start Pin
    start_popup = f"<b>🚩 Starting Location</b><br/>{start_name}"
    if start_accuracy_m is not None:
        start_popup += f"<br/>Browser-reported accuracy: ±{start_accuracy_m:.0f} m"
    folium.Marker(
        location=[start_lat, start_lon],
        popup=folium.Popup(start_popup, max_width=300),
        tooltip=f"Start: {start_name[:40]}...",
        icon=folium.Icon(color="green", icon="play", prefix="glyphicon"),
    ).add_to(m)

    # 5. Primary Destination Pin
    folium.Marker(
        location=[dest_lat, dest_lon],
        popup=folium.Popup(f"<b>🏁 Primary Destination</b><br/>{dest_name}", max_width=300),
        tooltip=f"Destination: {dest_name[:40]}...",
        icon=folium.Icon(color="red", icon="flag", prefix="glyphicon"),
    ).add_to(m)

    # Auto-fit bounds
    if all_lats and all_lons:
        m.fit_bounds([[min(all_lats), min(all_lons)], [max(all_lats), max(all_lons)]])

    return m


def add_navigation_overlay(
    folium_map,
    instructions: list,
    route_coords: list,
    navigation_key: str,
    allow_gps: bool,
):
    """Adds browser-side speech and route progress controls to a Folium map."""
    payload = {
        "key": navigation_key,
        "instructions": instructions,
        "routeCoords": route_coords,
        "allowGps": allow_gps,
    }
    payload_json = json.dumps(payload, ensure_ascii=True).replace("<", "\\u003c")
    control_html = """
    <style>
      .nav-card {
        position: absolute; z-index: 10000; top: 12px; left: 12px;
        width: min(390px, calc(100% - 40px)); box-sizing: border-box;
        padding: 14px; border: 1px solid #d8dfe8; border-radius: 12px;
        background: #fffdfb; box-shadow: 0 3px 14px #10243a33; color: #202a36;
        font: 14px/1.4 Arial, sans-serif;
      }
      .nav-card strong { display: block; margin-bottom: 5px; }
      .nav-status { color: #526171; font-size: 12px; margin: 7px 0; }
      .nav-controls { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 9px; }
      .nav-controls button {
        min-height: 38px; border: 1px solid #014baa; border-radius: 7px;
        padding: 7px 10px; background: #014baa; color: #fff;
        font: inherit; font-weight: 600; cursor: pointer;
      }
      .nav-controls button:hover:not(:disabled) { border-color: #013d8c; background: #013d8c; }
      .nav-card select {
        min-height: 38px; max-width: 100%; border: 1px solid #b9c7d7;
        border-radius: 7px; padding: 6px 8px; background: #fffdfb;
        color: #202a36; font: inherit;
      }
      .nav-controls button:focus-visible, .nav-card select:focus-visible,
      .nav-card input:focus-visible { outline: 3px solid #014baa66; outline-offset: 2px; }
      .nav-card label { display: inline-flex; align-items: center; gap: 5px; margin-top: 5px; }
      .nav-current { font-size: 15px; }
    </style>
    <section class="nav-card" aria-label="Voice navigation">
      <strong>🧭 Voice Navigation — <span id="nav-mode-label"></span></strong>
      <div id="nav-progress" class="nav-status"></div>
      <div id="nav-current" class="nav-current" aria-live="polite"></div>
      <div id="nav-status" class="nav-status"></div>
      <label id="nav-gps-option"><input id="nav-use-gps" type="checkbox"> Use live GPS (optional)</label>
      <div><label for="nav-voice">Supported browser voice:</label></div>
      <select id="nav-voice" aria-label="Supported browser voice"></select>
      <div class="nav-controls">
        <button id="nav-start" type="button">Start Voice Navigation</button>
        <button id="nav-pause" type="button">Pause</button>
        <button id="nav-stop" type="button">Stop Navigation</button>
        <button id="nav-next" type="button">Next Instruction</button>
        <button id="nav-mute" type="button">Mute</button>
      </div>
      <div class="nav-status">Browser speech only. Location is used only after you opt in and grant permission.</div>
    </section>
    """
    folium_map.get_root().html.add_child(folium.Element(control_html))
    map_name = folium_map.get_name()
    script = """
    (function () {
      const data = __NAV_DATA__;
      const map = __MAP_NAME__;
      const key = "evacuation-navigation:" + data.key;
      const $ = (id) => document.getElementById(id);
      const speechAvailable = "speechSynthesis" in window && "SpeechSynthesisUtterance" in window;
      let state;
      try {
        state = JSON.parse(localStorage.getItem(key) || "null");
      } catch (_) {
        state = null;
      }
      state = state || {index: 0, active: false, paused: false, muted: false, voiceURI: "", lastSpoken: -1};
      state.index = Math.max(0, Math.min(state.index || 0, data.instructions.length - 1));
      const wasActive = state.active;
      state.active = wasActive;
      state.paused = wasActive;
      state.lastSpoken = Number.isInteger(state.lastSpoken) ? state.lastSpoken : -1;
      let watchId = null;
      let gpsReady = false;
      let locationMarker = null;
      let lastGpsProgressPoint = null;
      let gpsProgressIndex = 0;
      let speechToken = 0;

      function save() {
        try { localStorage.setItem(key, JSON.stringify(state)); } catch (_) {}
      }
      function setStatus(message) { $("nav-status").textContent = message; }
      function currentInstruction() { return data.instructions[state.index]; }
      function render() {
        const step = currentInstruction();
        $("nav-current").textContent = step ? step.instruction : "No navigation instructions available.";
        $("nav-progress").textContent = step
          ? "Instruction " + (state.index + 1) + " of " + data.instructions.length
          : "No instructions";
        $("nav-pause").textContent = state.paused ? "Resume Navigation" : "Pause Navigation";
        $("nav-mute").textContent = state.muted ? "Unmute" : "Mute";
        save();
      }
      function stopWatching() {
        if (watchId !== null && navigator.geolocation) navigator.geolocation.clearWatch(watchId);
        watchId = null;
      }
      function cancelSpeech() {
        speechToken += 1;
        if (speechAvailable) window.speechSynthesis.cancel();
      }
      function speak(force) {
        if (!state.active || state.paused || !currentInstruction()) return;
        if (state.lastSpoken === state.index && !force) return;
        if (state.muted) { setStatus("Muted. Visual instructions remain available."); return; }
        if (!speechAvailable) { setStatus("SpeechSynthesis is not supported in this browser. Use the visual instructions."); return; }
        state.lastSpoken = state.index;
        save();
        cancelSpeech();
        const utteranceToken = speechToken;
        const utterance = new SpeechSynthesisUtterance(currentInstruction().instruction);
        const selected = window.speechSynthesis.getVoices().find((voice) => voice.voiceURI === $("nav-voice").value);
        if (selected) utterance.voice = selected;
        utterance.onerror = (event) => {
          if (utteranceToken === speechToken && event.error !== "interrupted" && event.error !== "canceled")
            setStatus("Browser speech failed (" + event.error + "). Use the visual or manual instructions.");
        };
        utterance.onstart = () => {
          if (state.mode === "gps") setStatus("GPS-guided progress; instruction announced once.");
        };
        window.speechSynthesis.speak(utterance);
      }
      function loadVoices() {
        const select = $("nav-voice");
        const previous = state.voiceURI || select.value;
        const voices = speechAvailable ? window.speechSynthesis.getVoices() : [];
        select.replaceChildren();
        if (!voices.length) {
          const option = document.createElement("option");
          option.textContent = speechAvailable ? "Browser default voice" : "Speech unavailable";
          option.value = "";
          select.appendChild(option);
        } else {
          voices.forEach((voice) => {
            const option = document.createElement("option");
            option.value = voice.voiceURI;
            option.textContent = voice.name + " (" + voice.lang + ")";
            select.appendChild(option);
          });
          select.value = voices.some((voice) => voice.voiceURI === previous) ? previous : voices[0].voiceURI;
        }
        state.voiceURI = select.value;
        save();
      }
      function distanceMeters(a, b) {
        const rad = (value) => value * Math.PI / 180;
        const dLat = rad(b[0] - a[0]), dLon = rad(b[1] - a[1]);
        const value = Math.sin(dLat / 2) ** 2 + Math.cos(rad(a[0])) * Math.cos(rad(b[0])) * Math.sin(dLon / 2) ** 2;
        return 6371000 * 2 * Math.atan2(Math.sqrt(value), Math.sqrt(1 - value));
      }
      function closestRoutePoint(point) {
        let closest = {index: -1, distance: Infinity};
        data.routeCoords.forEach((coord, index) => {
          const distance = distanceMeters(point, coord);
          if (distance < closest.distance) closest = {index: index, distance: distance};
        });
        return closest;
      }
      function beginManual(message) {
        stopWatching();
        state.mode = "manual";
        state.active = true;
        state.paused = false;
        $("nav-mode-label").textContent = "Manual/demo";
        setStatus(message || "Manual/demo mode: location is not being tracked. Use Next Instruction.");
        render();
        speak(false);
      }
      function advance() {
        if (!state.active || state.paused) return;
        if (state.index >= data.instructions.length - 1) {
          setStatus("End of available route instructions. Navigation complete for this demo.");
          state.active = false;
          stopWatching();
          render();
          return;
        }
        state.index += 1;
        render();
        speak(false);
      }
      function startGps() {
        stopWatching();
        if (!navigator.geolocation) {
          beginManual("Live GPS is unavailable in this browser. Manual/demo navigation is active; use Next Instruction.");
          return;
        }
        state.mode = "gps";
        state.active = true;
        state.paused = false;
        gpsReady = false;
        lastGpsProgressPoint = null;
        gpsProgressIndex = 0;
        $("nav-mode-label").textContent = "Live GPS";
        setStatus("Waiting for an accurate GPS fix near the mapped route. No progress is assumed.");
        render();
        try {
          watchId = navigator.geolocation.watchPosition((position) => {
          if (!state.active || state.paused) return;
          const point = [position.coords.latitude, position.coords.longitude];
          const accuracy = position.coords.accuracy;
          if (!Number.isFinite(accuracy) || accuracy > 35) {
            setStatus("GPS accuracy is too low to announce a turn. Waiting; use Next Instruction for manual control.");
            return;
          }
          if (!locationMarker) {
            locationMarker = L.circleMarker(point, {radius: 7, color: "#0f172a", fillColor: "#38bdf8", fillOpacity: 1}).addTo(map);
          } else {
            locationMarker.setLatLng(point);
          }
          if (!gpsReady) {
            const start = data.routeCoords[0];
            const routeDistance = closestRoutePoint(point);
            if (!start || distanceMeters(point, start) > Math.max(60, accuracy * 1.5)
                || routeDistance.distance > Math.max(60, accuracy * 1.5)) {
              setStatus("GPS fix is not near the route start. No navigation progress is assumed.");
              return;
            }
            gpsReady = true;
            gpsProgressIndex = routeDistance.index;
            lastGpsProgressPoint = point;
            setStatus("GPS position is near the route start. Progress instructions will be announced once.");
            speak(false);
            return;
          }
          const routePosition = closestRoutePoint(point);
          if (routePosition.distance > Math.max(30, accuracy * 1.5)) {
            setStatus("GPS is too far from the mapped route for reliable progress. Use Next Instruction if needed.");
            return;
          }
          gpsProgressIndex = Math.max(gpsProgressIndex, routePosition.index);
          if (lastGpsProgressPoint && distanceMeters(point, lastGpsProgressPoint) < Math.max(15, accuracy * 0.4)) return;
          lastGpsProgressPoint = point;
          const nextIndex = state.index + 1;
          const nextStep = data.instructions[nextIndex];
          if (!nextStep || !nextStep.location) {
            setStatus("The next maneuver has no reliable GPS coordinate. Use Next Instruction for manual progression.");
            return;
          }
          const threshold = Math.max(20, Math.min(45, accuracy * 1.25));
          const targetPosition = closestRoutePoint(nextStep.location);
          if (gpsProgressIndex >= targetPosition.index && distanceMeters(point, nextStep.location) <= threshold) advance();
        }, (error) => {
          const reasons = {1: "Location permission was denied", 2: "GPS position is unavailable", 3: "GPS request timed out"};
          beginManual((reasons[error.code] || "GPS failed") + ". Manual/demo navigation is active; use Next Instruction.");
        }, {enableHighAccuracy: true, maximumAge: 2000, timeout: 15000});
        } catch (error) {
          const detail = error && error.message ? " (" + error.message + ")" : "";
          beginManual("Live GPS could not be started" + detail + ". Manual/demo navigation is active; use Next Instruction.");
        }
      }

      $("nav-mode-label").textContent = data.allowGps ? "Manual/demo until started" : "Manual/demo only";
      $("nav-gps-option").hidden = !data.allowGps;
      $("nav-next").hidden = data.instructions.length === 0;
      if (wasActive) {
        state.index = Math.max(0, Math.min(state.index, data.instructions.length - 1));
        state.mode = "manual";
        $("nav-mode-label").textContent = "Manual/demo";
        setStatus("Navigation was paused by a page refresh. Resume manually; GPS tracking must be restarted.");
      } else {
        setStatus("Ready. Start navigation to hear the first instruction.");
      }
      loadVoices();
      if (speechAvailable) window.speechSynthesis.onvoiceschanged = loadVoices;
      $("nav-voice").addEventListener("change", () => { state.voiceURI = $("nav-voice").value; save(); });
      $("nav-start").addEventListener("click", () => {
        if (!state.active) { state.index = 0; state.lastSpoken = -1; }
        if (data.allowGps && $("nav-use-gps").checked) startGps();
        else beginManual("Manual/demo mode: location is not being tracked. Use Next Instruction.");
      });
      $("nav-pause").addEventListener("click", () => {
        if (!state.active) { setStatus("Start navigation before pausing or resuming."); return; }
        state.paused = !state.paused;
        if (speechAvailable) {
          if (state.paused) window.speechSynthesis.pause();
          else window.speechSynthesis.resume();
        }
        render();
        setStatus(state.paused ? "Navigation paused." : "Navigation resumed. GPS progress continues only with accurate fixes.");
        if (!state.paused) speak(true);
      });
      $("nav-stop").addEventListener("click", () => {
        stopWatching();
        cancelSpeech();
        state.active = false; state.paused = false; state.index = 0; state.lastSpoken = -1;
        gpsReady = false;
        setStatus("Navigation stopped.");
        render();
      });
      $("nav-next").addEventListener("click", () => {
        if (!state.active) beginManual("Manual/demo navigation: location is not being tracked. Use Next Instruction.");
        else if (state.paused) setStatus("Resume navigation before advancing.");
        else advance();
      });
      $("nav-mute").addEventListener("click", () => {
        state.muted = !state.muted;
        if (state.muted) cancelSpeech();
        render();
        if (state.muted) setStatus("Muted. Visual instructions remain available.");
        else {
          setStatus("Unmuted. The next instruction can be spoken.");
          speak(false);
        }
      });
      render();
      if (!speechAvailable) setStatus("SpeechSynthesis is not supported in this browser. Manual visual instructions remain available.");
    })();
    """
    folium_map.get_root().script.add_child(
        folium.Element(
            script.replace("__NAV_DATA__", payload_json).replace("__MAP_NAME__", map_name)
        )
    )
    components.html(folium_map.get_root().render(), height=650, scrolling=False)


def build_simulated_network_map(graph, start_node, dest_node, blocked_edges, current_route=None, alt_routes=None):
    """Builds interactive Folium map for the simulated 6-node graph."""
    evacuation_map = folium.Map(
        location=SIMULATED_MAP_CENTER,
        zoom_start=13,
        tiles="OpenStreetMap",
        attr='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
        control_scale=True,
    )

    blocked_set = set(tuple(sorted([u, v])) for u, v in blocked_edges)
    route_edges = set()
    if current_route and len(current_route) >= 2:
        for i in range(len(current_route) - 1):
            route_edges.add(tuple(sorted([current_route[i], current_route[i + 1]])))

    # 1. Base open roads (Blue)
    for u, v, data in graph.edges(data=True):
        edge = tuple(sorted([u, v]))
        if edge not in blocked_set and edge not in route_edges:
            folium.PolyLine(
                locations=[SIMULATED_COORDINATES[u], SIMULATED_COORDINATES[v]],
                color="#3182ce",
                weight=4,
                opacity=0.6,
                tooltip=f"Road {u} <--> {v} | Distance: {data['distance']} units",
            ).add_to(evacuation_map)

    # 2. Blocked roads (Red dashed)
    for u, v in blocked_edges:
        if u in SIMULATED_COORDINATES and v in SIMULATED_COORDINATES:
            folium.PolyLine(
                locations=[SIMULATED_COORDINATES[u], SIMULATED_COORDINATES[v]],
                color="#e53e3e",
                weight=6,
                dash_array="8, 8",
                opacity=0.9,
                tooltip=f"⛔ BLOCKED ROAD: {u} <--> {v} (Closed)",
            ).add_to(evacuation_map)

    # 3. Active Primary Evacuation Route (Green)
    if current_route and len(current_route) >= 2:
        coords = [SIMULATED_COORDINATES[n] for n in current_route]
        folium.PolyLine(
            locations=coords,
            color="#2f855a",
            weight=7,
            opacity=0.95,
            tooltip=f"✅ PRIMARY OPTIMAL ROUTE: {' -> '.join(current_route)}",
        ).add_to(evacuation_map)

    # 4. Markers
    for node, data in graph.nodes(data=True):
        coord = SIMULATED_COORDINATES.get(node)
        if not coord:
            continue
        role = data.get("role", "Intersection")
        if node == start_node:
            color, icon = "green", "play"
        elif node == dest_node:
            color, icon = "red", "shield"
        elif current_route and node in current_route:
            color, icon = "darkgreen", "arrow-up"
        else:
            color, icon = "blue", "info-sign"

        folium.Marker(
            location=coord,
            popup=f"<b>Intersection {node}</b><br/>Role: {role}",
            tooltip=f"Intersection {node} ({role})",
            icon=folium.Icon(color=color, icon=icon, prefix="glyphicon"),
        ).add_to(evacuation_map)

    return evacuation_map


def render_multi_route_legend(routes_dict: dict, hazard_info: dict = None, shelter_info: dict = None):
    """
    Renders a compact, responsive, and clearly labeled legend for the Multi-Route map.
    Eliminates CommonMark code-block indentation bugs and pairs each symbol with clear text.
    """
    route_items = [
        '<span style="display:inline-flex; align-items:center; gap:6px;">'
        '<span style="display:inline-block; width:13px; height:13px; border-radius:3px; background-color:#2f855a;"></span>'
        '<span>🟢 <b>Shortest-Distance Route</b></span></span>',
        '<span style="display:inline-flex; align-items:center; gap:6px;">'
        '<span style="display:inline-block; width:13px; height:13px; border-radius:3px; background-color:#3182ce;"></span>'
        '<span>🔵 <b>Alternative Route 1</b></span></span>',
        '<span style="display:inline-flex; align-items:center; gap:6px;">'
        '<span style="display:inline-block; width:13px; height:13px; border-radius:3px; background-color:#805ad5;"></span>'
        '<span>🟣 <b>Alternative Route 2</b></span></span>',
    ]

    if "hazard_avoid" in routes_dict:
        route_items.append(
            '<span style="display:inline-flex; align-items:center; gap:6px;">'
            '<span style="display:inline-block; width:13px; height:13px; border-radius:3px; background-color:#dd6b20;"></span>'
            '<span>🟠 <b>Hazard-Avoiding Route</b></span></span>'
        )

    if "nearest_shelter" in routes_dict:
        route_items.append(
            '<span style="display:inline-flex; align-items:center; gap:6px;">'
            '<span style="display:inline-block; width:13px; height:13px; border-radius:3px; background-color:#319795;"></span>'
            '<span>🔷 <b>Nearest Reachable Shelter Route</b></span></span>'
        )

    marker_items = [
        '<span style="display:inline-flex; align-items:center; gap:5px;">🚩 <span><b>Starting Location</b> (Green Pin)</span></span>',
        '<span style="display:inline-flex; align-items:center; gap:5px;">🏁 <span><b>Safe Destination</b> (Red Pin)</span></span>',
    ]

    if shelter_info and shelter_info.get("lat"):
        marker_items.append(
            '<span style="display:inline-flex; align-items:center; gap:5px;">🛡️ <span><b>Candidate Shelter</b> (User-Provided / Unverified)</span></span>'
        )

    if hazard_info and hazard_info.get("lat"):
        marker_items.append(
            '<span style="display:inline-flex; align-items:center; gap:5px;">⚠️ <span><b>Simulated Hazard Zone</b> (Danger Buffer)</span></span>'
        )

    routes_html = "".join(f'<div style="margin: 2px 12px 2px 0;">{item}</div>' for item in route_items)
    markers_html = "".join(f'<div style="margin: 2px 12px 2px 0;">{item}</div>' for item in marker_items)

    legend_html = (
        '<div style="background-color: #f8fafc; border: 1px solid #cbd5e1; border-radius: 6px; '
        'padding: 8px 12px; margin-bottom: 10px; font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif; font-size: 13px; color: #1e293b;">'
        '<div style="display: flex; flex-wrap: wrap; align-items: center; margin-bottom: 4px; padding-bottom: 4px; border-bottom: 1px dashed #e2e8f0;">'
        '<span style="font-weight: 700; color: #0f172a; margin-right: 10px; font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.5px;">Routes:</span>'
        f'{routes_html}'
        '</div>'
        '<div style="display: flex; flex-wrap: wrap; align-items: center;">'
        '<span style="font-weight: 700; color: #0f172a; margin-right: 10px; font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.5px;">Markers:</span>'
        f'{markers_html}'
        '</div>'
        '</div>'
    )
    st.markdown(legend_html, unsafe_allow_html=True)


def render_simulated_network_legend():
    """Renders a clean, compact legend for the Academic Simulated Network map."""
    legend_html = (
        '<div style="background-color: #f8fafc; border: 1px solid #cbd5e1; border-radius: 6px; '
        'padding: 8px 12px; margin-bottom: 10px; font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif; font-size: 13px; color: #1e293b; display: flex; flex-wrap: wrap; gap: 14px; align-items: center;">'
        '<span>🟢 <b>Active Evacuation Route</b></span>'
        '<span>🔵 <b>Passable Open Roads</b></span>'
        '<span>🔴 <b>Blocked Roads (Red Dashed)</b></span>'
        '<span>🚩 <b>Start Intersection</b></span>'
        '<span>🛡️ <b>Safe Shelter Destination</b></span>'
        '</div>'
    )
    st.markdown(legend_html, unsafe_allow_html=True)


def render_route_cards(cards):
    """Displays route and algorithm results in responsive, accessible cards."""
    card_html = []
    for card in cards:
        selected_class = " is-selected" if card.get("selected") else ""
        accent = html.escape(card.get("accent", "#0f766e"), quote=True)
        title = html.escape(str(card["title"]))
        category = html.escape(str(card["category"]))
        status = html.escape(str(card["status"]))
        metrics = "".join(
            '<div class="aegis-route-metric">'
            f'<span>{html.escape(str(label))}</span>'
            f'<strong>{html.escape(str(value))}</strong>'
            "</div>"
            for label, value in card.get("metrics", [])
        )
        details = "".join(
            f'<p>{html.escape(str(detail))}</p>'
            for detail in card.get("details", [])
        )
        selected_badge = (
            '<span class="aegis-selected-badge">Selected</span>'
            if card.get("selected")
            else ""
        )
        card_html.append(
            f'<article class="aegis-route-card{selected_class}" style="--route-accent:{accent}">'
            '<div class="aegis-route-card-heading">'
            f'<div><span class="aegis-route-category">{category}</span><h3>{title}</h3></div>'
            f"{selected_badge}</div>"
            f'<div class="aegis-route-metrics">{metrics}</div>'
            f'<p class="aegis-route-status">{status}</p>{details}</article>'
        )

    if card_html:
        st.markdown(
            '<div class="aegis-route-grid">' + "".join(card_html) + "</div>",
            unsafe_allow_html=True,
        )


def render_weather_report(location_label: str, report: dict):
    """Displays Open-Meteo observations and forecast without interpreting safety."""
    if not report.get("success"):
        st.warning(f"{location_label}: {report.get('error', 'Weather data is unavailable.')}")
        return

    retrieved_at = report.get("retrieved_at_utc")
    if isinstance(retrieved_at, str):
        try:
            retrieval_time = datetime.fromisoformat(
                retrieved_at.replace("Z", "+00:00")
            )
            if retrieval_time.tzinfo is None:
                retrieval_time = retrieval_time.replace(tzinfo=timezone.utc)
            age_seconds = max(
                0,
                (datetime.now(timezone.utc) - retrieval_time).total_seconds(),
            )
            age_minutes = int(age_seconds // 60)
            if age_seconds > 30 * 60:
                st.warning(
                    "STALE cached weather report — the last successful Open-Meteo "
                    f"response was retrieved {age_minutes} minutes ago "
                    f"({retrieved_at}). Refresh before relying on this information."
                )
            else:
                st.caption(
                    "Saved Open-Meteo response (reused on Streamlit reruns) · "
                    f"last API retrieval {age_minutes} minutes ago at {retrieved_at}. "
                    "Use the weather button to request a fresh response."
                )
        except ValueError:
            st.warning(
                "Weather response retrieval time is invalid. Refresh to obtain "
                "a report with a verifiable retrieval timestamp."
            )
    else:
        st.warning(
            "Weather response retrieval time is unavailable. Refresh to obtain "
            "a report with a verifiable retrieval timestamp."
        )

    current = report.get("current", {})
    timezone_label = (
        report.get("timezone_abbreviation")
        or report.get("timezone")
        or "local time"
    )
    coordinates = (
        f"{report.get('latitude', '—')}, {report.get('longitude', '—')}"
    )
    icon = current.get("icon", "🌡️")
    description = current.get("weather_description", "Current conditions unavailable")

    with st.container(border=True):
        st.markdown(f"#### {icon} {location_label}")
        st.caption(
            f"Coordinates: {coordinates} · Report time: "
            f"{current.get('time') or 'not provided'} ({timezone_label})"
        )
        st.markdown(f"**{description}**")
        metric_columns = st.columns(4)
        metric_data = [
            ("Temperature", current.get("temperature_c"), "°C"),
            ("Precipitation", current.get("precipitation_mm"), " mm"),
            ("Rainfall", current.get("rain_mm"), " mm"),
            ("Wind speed", current.get("wind_speed_kmh"), " km/h"),
        ]
        for column, (label, value, unit) in zip(metric_columns, metric_data):
            with column:
                st.metric(
                    label,
                    f"{value}{unit}" if value is not None else "Unavailable",
                )

        extra_conditions = []
        if current.get("apparent_temperature_c") is not None:
            extra_conditions.append(
                f"Feels like {current['apparent_temperature_c']} °C"
            )
        if current.get("relative_humidity_percent") is not None:
            extra_conditions.append(
                f"Humidity {current['relative_humidity_percent']}%"
            )
        if current.get("wind_direction_degrees") is not None:
            extra_conditions.append(
                f"Wind direction {current['wind_direction_degrees']}°"
            )
        if extra_conditions:
            st.caption(" · ".join(extra_conditions))

        for warning in report.get("warnings", []):
            st.info(warning)

        hourly = report.get("hourly", [])
        if hourly:
            st.markdown("**Upcoming hourly forecast**")
            hourly_rows = [
                {
                    "Local time": item.get("time") or "—",
                    "Conditions": (
                        f"{item.get('icon', '🌡️')} {item.get('weather_description')}"
                    ),
                    "Temperature (°C)": item.get("temperature_c"),
                    "Precipitation chance (%)": item.get(
                        "precipitation_probability_percent"
                    ),
                    "Precipitation (mm)": item.get("precipitation_mm"),
                    "Rain (mm)": item.get("rain_mm"),
                    "Wind (km/h)": item.get("wind_speed_kmh"),
                }
                for item in hourly
            ]
            st.dataframe(hourly_rows, hide_index=True, use_container_width=True)
        else:
            st.info("Hourly forecast data is unavailable for this response.")

        st.markdown(
            '[Weather data by Open-Meteo.com](https://open-meteo.com/) · '
            '[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)',
            help="Open-Meteo forecast data are licensed under CC BY 4.0.",
        )


@st.fragment(run_every="60s")
def render_emergency_monitoring_panel(
    targets: list,
    refresh_interval_minutes: int,
    rainfall_threshold_mm: float,
    official_targets: Optional[list] = None,
    offline_mode: bool = False,
):
    """Render forecast indicators and official-alert status for selected locations."""
    refresh_requested = st.button(
        "Refresh Emergency Data",
        key="refresh_emergency_monitor_data",
        help=(
            "Refreshes weather, flood, and NDMA SACHET reports for selected "
            "real-world locations."
        ),
        disabled=offline_mode,
    )
    if offline_mode:
        st.info(
            "Offline Mode is enabled. Forecast services are not contacted. "
            "Any saved reports below are cached snapshots, not current conditions."
        )
        for label, latitude, longitude, location_name in targets:
            st.markdown(f"**{label}: {location_name}**")
            weather = load_cached_report("weather", latitude, longitude)
            flood = load_cached_report("flood", latitude, longitude)
            if weather:
                st.warning("Cached weather snapshot — freshness cannot be confirmed offline.")
                render_weather_report(label, weather)
            else:
                st.caption("Weather: UNAVAILABLE — no locally saved report.")
            if flood:
                st.warning("Cached river-discharge forecast — freshness cannot be confirmed offline.")
                retrieved_at = flood.get("retrieved_at_utc") or flood.get("checked_at_utc")
                st.caption(
                    f"Open-Meteo · retrieved {retrieved_at or 'time unavailable'}"
                )
                daily = flood.get("daily")
                if flood.get("success") and isinstance(daily, list) and daily:
                    st.dataframe(
                        [
                            {
                                "Forecast date": item.get("date", "—"),
                                "River discharge (m³/s)": item.get(
                                    "river_discharge_m3s",
                                    "Unavailable",
                                ),
                            }
                            for item in daily
                            if isinstance(item, dict)
                        ],
                        hide_index=True,
                        use_container_width=True,
                    )
                else:
                    st.caption(flood.get("error", "Flood forecast unavailable."))
                st.caption(
                    "River discharge is not a direct measurement of street flooding."
                )
            else:
                st.caption("Flood forecast: UNAVAILABLE — no locally saved report.")
        st.caption(
            "Offline mode does not infer safety from missing or old forecast data."
        )
        return

    now = datetime.now(timezone.utc)
    last_refresh_value = st.session_state.get("emergency_monitor_last_refresh_utc")
    last_refresh = None
    if isinstance(last_refresh_value, str):
        try:
            last_refresh = datetime.fromisoformat(
                last_refresh_value.replace("Z", "+00:00")
            )
        except ValueError:
            last_refresh = None

    auto_refresh_due = (
        refresh_interval_minutes > 0
        and (
            last_refresh is None
            or (now - last_refresh).total_seconds()
            >= refresh_interval_minutes * 60
        )
    )
    existing_reports = st.session_state.get("emergency_monitor_reports", {})
    missing_target_report = any(
        not isinstance(existing_reports, dict)
        or not isinstance(existing_reports.get(
            f"{latitude:.6f},{longitude:.6f}"
        ), dict)
        or not isinstance(
            existing_reports[
                f"{latitude:.6f},{longitude:.6f}"
            ].get("weather"),
            dict,
        )
        or not isinstance(
            existing_reports[
                f"{latitude:.6f},{longitude:.6f}"
            ].get("flood"),
            dict,
        )
        for _, latitude, longitude, _ in targets
    )
    first_check = last_refresh is None
    if refresh_requested or auto_refresh_due or missing_target_report or first_check:
        with st.spinner("Checking forecast indicators for selected locations..."):
            reports = refresh_forecast_reports(
                targets,
                force_refresh=(
                    refresh_requested
                    or (auto_refresh_due and last_refresh is not None)
                ),
            )
        if refresh_requested:
            with st.spinner("Checking official disaster alerts..."):
                refresh_official_alert_reports(
                    official_targets if official_targets is not None else targets,
                    force_refresh=True,
                )
        now = datetime.now(timezone.utc)
        st.session_state["emergency_monitor_last_refresh_utc"] = now.isoformat()
        previous_active = st.session_state.get("active_emergency_alerts", {})
        if not isinstance(previous_active, dict):
            previous_active = {}
        current_active = {}
        target_keys = {
            f"{latitude:.6f},{longitude:.6f}"
            for _, latitude, longitude, _ in targets
        }
        for old_id, old_alert in previous_active.items():
            if old_alert.get("coordinate_key") not in target_keys:
                current_active[old_id] = old_alert

        for label, latitude, longitude, location_name in targets:
            coordinate_key = f"{latitude:.6f},{longitude:.6f}"
            entry = reports.get(coordinate_key, {})
            weather_report = entry.get("weather")
            flood_report = entry.get("flood")
            assessment = assess_emergency_conditions(
                weather_report,
                flood_report,
                rainfall_threshold_mm=rainfall_threshold_mm,
                now=now,
            )
            if not assessment["weather_fresh"]:
                for old_id, old_alert in previous_active.items():
                    if old_alert.get("coordinate_key") == coordinate_key:
                        current_active[old_id] = old_alert
                continue
            for alert in assessment["alerts"]:
                alert_id = emergency_alert_id(alert, latitude, longitude)
                current_active[alert_id] = {
                    "coordinate_key": coordinate_key,
                    "location": location_name,
                    "label": label,
                    "alert": alert,
                }

        reconciliation = reconcile_active_alerts(previous_active, current_active)
        st.session_state["active_emergency_alerts"] = current_active
        st.session_state["cleared_emergency_alerts"] = reconciliation["cleared"]
        if refresh_requested:
            st.rerun(scope="app")

    st.caption(
        "Open-Meteo weather and GloFAS-derived river-discharge forecasts are point/grid-cell "
        "indicators, not official warnings, local gauge observations, or validated flood probabilities. "
        "No route is changed or declared safe from these indicators."
    )
    st.markdown(
        "**Official alerts:** NDMA SACHET location reports are available for India "
        "through the alert panel below. IMD and Nepal DHM machine-readable alert "
        "feeds are not connected. Check official agencies for verified instructions."
    )
    st.markdown(
        '[Open-Meteo forecast](https://open-meteo.com/) · '
        '[Flood API documentation](https://open-meteo.com/en/docs/flood-api) · '
        '[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)',
        help="Open-Meteo data are provided under CC BY 4.0; free API use is for non-commercial purposes.",
    )
    st.caption(
        "The flood model returns discharge for a nearby river on a roughly 5 km grid; "
        "it does not provide an affected-area polygon, local danger threshold, or verified road closure. "
        "Route-corridor intersection checks are therefore unavailable."
    )

    saved_reports = st.session_state.get("emergency_monitor_reports", {})
    if not isinstance(saved_reports, dict):
        saved_reports = {}
    seen_alerts = st.session_state.setdefault("seen_emergency_alert_ids", [])
    if not isinstance(seen_alerts, list):
        seen_alerts = []
        st.session_state["seen_emergency_alert_ids"] = seen_alerts

    cleared_alerts = st.session_state.get("cleared_emergency_alerts", [])
    if cleared_alerts:
        with st.container(border=True):
            st.markdown("**No longer present in the latest checked forecast**")
            for cleared in cleared_alerts:
                alert = cleared.get("alert", {})
                st.caption(
                    f"{cleared.get('location', 'Selected location')}: "
                    f"{alert.get('hazard', 'Forecast indicator')} for "
                    f"{alert.get('timing', 'an unspecified time')} is no longer "
                    "above the configured screening trigger. This does not mean "
                    "that an official warning has been cancelled."
                )

    for label, latitude, longitude, location_name in targets:
        coordinate_key = f"{latitude:.6f},{longitude:.6f}"
        saved_entry = saved_reports.get(coordinate_key)
        with st.container(border=True):
            st.markdown(f"#### {label}: {location_name}")
            st.caption(f"Selected coordinates: {latitude:.6f}, {longitude:.6f}")
            if not isinstance(saved_entry, dict):
                source_columns = st.columns(2)
                for column, source_name in zip(
                    source_columns,
                    ("Weather forecast", "Flood forecast"),
                ):
                    with column:
                        st.metric(source_name, "Unknown")
                        st.caption("No source data checked for this location.")
                st.info(
                    "Status: **Unknown** — no data has been checked for this location. "
                    "Press Refresh emergency data or enable automatic refresh."
                )
                continue

            weather_report = saved_entry.get("weather")
            flood_report = saved_entry.get("flood")
            official_alert_reports = st.session_state.get(
                "official_alert_reports",
                {},
            )
            official_coordinate_key = f"{latitude:.5f},{longitude:.5f}"
            official_entry = (
                official_alert_reports.get(official_coordinate_key, {})
                if isinstance(official_alert_reports, dict)
                else {}
            )
            official_report = (
                official_entry.get("last_success")
                if isinstance(official_entry, dict)
                else None
            )
            official_summary = classify_alert_report(official_report, now)
            assessment = assess_emergency_conditions(
                weather_report,
                flood_report,
                rainfall_threshold_mm=rainfall_threshold_mm,
                now=now,
            )
            if assessment["status"] == "Watch":
                st.warning(
                    f"Emergency screening: **Watch** — {assessment['status_reason']}"
                )
            else:
                st.info(
                    f"Emergency screening: **Unknown** — {assessment['status_reason']}"
                )

            source_columns = st.columns(2)
            for column, source_name, report, last_success, fresh in (
                (
                    source_columns[0],
                    "Weather forecast",
                    weather_report,
                    saved_entry.get("weather_last_success"),
                    assessment["weather_fresh"],
                ),
                (
                    source_columns[1],
                    "Flood forecast · river discharge",
                    flood_report,
                    saved_entry.get("flood_last_success"),
                    assessment["flood_fresh"],
                ),
            ):
                with column:
                    if fresh:
                        st.metric(source_name, "Available")
                        retrieved_at = (
                            report.get("retrieved_at_utc")
                            or report.get("checked_at_utc")
                            or "time unavailable"
                        )
                        st.caption(
                            f"Source: Open-Meteo · Retrieved {retrieved_at}"
                        )
                    else:
                        status = (
                            "Stale / unknown age"
                            if isinstance(report, dict) and report.get("success")
                            else "Unavailable"
                        )
                        st.metric(source_name, status)
                        error = (
                            report.get("error", "Data unavailable.")
                            if isinstance(report, dict)
                            else "Data unavailable."
                        )
                        if isinstance(report, dict) and report.get("success"):
                            st.caption(
                                f"{source_name}: stale or missing retrieval time · "
                                f"Last checked {report.get('retrieved_at_utc', 'unknown')}"
                            )
                        else:
                            st.caption(f"{source_name}: unavailable · {error}")
                            if isinstance(last_success, dict):
                                st.caption(
                                    "Last successful update: "
                                    f"{last_success.get('retrieved_at_utc', 'time unavailable')}"
                                )

            weather_display_report = (
                weather_report
                if isinstance(weather_report, dict) and weather_report.get("success")
                else saved_entry.get("weather_last_success")
            )
            if isinstance(weather_display_report, dict):
                current = weather_display_report.get("current", {})
                if isinstance(current, dict):
                    stale_label = (
                        "STALE cached forecast · "
                        if not assessment["weather_fresh"]
                        else ""
                    )
                    st.caption(
                        f"{stale_label}Current conditions: "
                        f"{current.get('weather_description', 'Unavailable')} · "
                        f"{current.get('temperature_c', 'Unavailable')} °C · "
                        f"precipitation {current.get('precipitation_mm', 'Unavailable')} mm · "
                        f"wind {current.get('wind_speed_kmh', 'Unavailable')} km/h"
                    )
                    st.caption(
                        "Weather report time: "
                        f"{current.get('time') or 'Unavailable'} · "
                        "Source: Open-Meteo Weather Forecast API"
                    )

            for warning in (
                weather_report.get("warnings", [])
                if isinstance(weather_report, dict)
                else []
            ):
                st.caption(f"Weather data note: {warning}")

            with st.container(border=True):
                official_status = official_summary["status"]
                official_status_labels = {
                    "active": (
                        f"{official_summary['active_count']} active report(s)"
                    ),
                    "scheduled": (
                        f"{official_summary['scheduled_count']} scheduled report(s)"
                    ),
                    "expired": (
                        f"{official_summary['expired_count']} expired only"
                    ),
                    "validity_unknown": (
                        f"{official_summary['validity_unknown_count']} "
                        "with unknown validity"
                    ),
                    "no_matching_alerts": "No matching alerts returned",
                    "stale": "Stale",
                    "unavailable": "Unavailable / not checked",
                }
                st.metric(
                    "Official disaster alerts",
                    official_status_labels[official_status],
                )
                if isinstance(official_report, dict):
                    st.caption(
                        "Source: NDMA SACHET · Last successful update: "
                        f"{official_report.get('retrieved_at_utc', 'time unavailable')}"
                    )
                if isinstance(official_entry, dict) and official_entry.get("last_error"):
                    st.caption(
                        "Latest alert refresh failed: "
                        f"{official_entry['last_error']}"
                    )
                if official_status == "no_matching_alerts":
                    st.caption(
                        "The location query returned no alerts. Feed coverage and "
                        "completeness are not established; this is not an all-clear."
                    )
                elif official_status == "unavailable":
                    st.caption(
                        (official_entry.get("last_error")
                         if isinstance(official_entry, dict)
                         else None)
                        or "Use Refresh Emergency Data or the alert check below."
                    )

            if assessment["alerts"]:
                for alert in assessment["alerts"]:
                    alert_id = emergency_alert_id(alert, latitude, longitude)
                    is_new = alert_id not in seen_alerts
                    if is_new:
                        seen_alerts.append(alert_id)
                    new_label = " · New since last check" if is_new else ""
                    st.warning(
                        f"**{alert['hazard']}{new_label}** — "
                        f"{alert['rainfall_mm']:g} mm forecast in the hour "
                        f"starting {alert['timing']}. "
                        f"Screening trigger: {alert['threshold_mm']:g} mm/hour. "
                        f"Certainty: {alert['certainty']}."
                    )
                    st.caption(
                        f"Source: {alert['source']} · {alert['data_kind']}. "
                        "This is a screening indicator, not an official warning."
                    )

            flood_display_report = (
                flood_report
                if isinstance(flood_report, dict) and flood_report.get("success")
                else saved_entry.get("flood_last_success")
            )
            if isinstance(flood_display_report, dict):
                stale_label = (
                    "STALE cached flood forecast · "
                    if not assessment["flood_fresh"]
                    else ""
                )
                st.caption(
                    f"{stale_label}Source: Open-Meteo Flood API · "
                    f"Retrieved {flood_display_report.get('retrieved_at_utc', 'time unavailable')}"
                )
                st.caption(
                    "Flood API grid cell: "
                    f"{flood_display_report.get('latitude', '—')}, "
                    f"{flood_display_report.get('longitude', '—')} · "
                    f"Timezone: {flood_display_report.get('timezone') or 'not provided'}"
                )
                daily_rows = [
                    {
                        "Forecast date": item["date"],
                        "River discharge (m³/s)": (
                            f"{item['river_discharge_m3s']:g} m³/s"
                        ),
                    }
                    for item in flood_display_report.get("daily", [])
                    if isinstance(item, dict)
                    and isinstance(item.get("date"), str)
                    and isinstance(item.get("river_discharge_m3s"), (int, float))
                ]
                if daily_rows:
                    st.dataframe(daily_rows, hide_index=True, use_container_width=True)
                else:
                    st.warning(
                        "Flood forecast unavailable: the provider response had no "
                        "usable daily river-discharge values."
                    )
                for warning in flood_display_report.get("warnings", []):
                    st.caption(f"Flood forecast data note: {warning}")
                st.caption(
                    "Discharge is shown as supplied; no local river threshold is available "
                    "to classify it as a warning or critical condition."
                )

            if not assessment["alerts"] and assessment["weather_fresh"]:
                st.caption(
                    "No forecast rainfall indicator met the configured screening trigger. "
                    "This does not rule out flooding or mean conditions are safe."
                )
            elif not assessment["alerts"]:
                st.caption(
                    "No fresh weather forecast is available to evaluate the rainfall trigger."
                )
            st.caption(
                "No source-provided confidence or verified emergency action guidance is available. "
                "Follow current official instructions."
            )


def render_official_alerts_panel(
    targets: list,
    routes_dict: dict,
    offline_mode: bool = False,
) -> list:
    """Display explicitly refreshed official alerts and return fresh map geometries."""
    st.subheader("Official disaster alerts")
    st.caption(
        "India: location-wise reports from the NDMA SACHET portal. "
        "This is separate from Open-Meteo forecasts. IMD and Nepal DHM "
        "machine-readable alert feeds are not connected."
    )
    st.markdown(
        f"[Open NDMA SACHET official alert portal]({SACHET_ALERT_PORTAL_URL}) · "
        "[NDMA](https://ndma.gov.in/) · [IMD](https://mausam.imd.gov.in/)"
    )
    st.caption(
        "Checks run only when requested. SACHET returns alerts for a 20 km search radius; "
        "AEGIS marks applicability and route overlap only from supplied alert polygons. "
        "A missing, empty, stale, or unavailable result is not evidence that a place "
        "or route is safe."
    )
    refresh = st.button(
        "Check official alerts for selected locations",
        key="check_official_disaster_alerts",
        help="Requests fresh SACHET reports for the selected real-world origin and destination.",
        disabled=offline_mode,
    )

    stored_reports = st.session_state.get("official_alert_reports", {})
    if not isinstance(stored_reports, dict):
        stored_reports = {}
    unique_targets = {}
    for label, latitude, longitude, location_name in targets:
        coordinate_key = f"{float(latitude):.5f},{float(longitude):.5f}"
        unique_targets.setdefault(
            coordinate_key,
            (label, float(latitude), float(longitude), location_name),
        )

    if offline_mode:
        st.warning(
            "Offline Mode is enabled. No alert source is contacted. Saved alert "
            "records are historical snapshots and cannot confirm current validity."
        )
        for coordinate_key, target in unique_targets.items():
            report = load_cached_report(
                "official_alerts",
                target[1],
                target[2],
            )
            if not report:
                st.caption(f"{target[0]}: OFFICIAL ALERTS UNAVAILABLE — no saved report.")
                continue
            st.markdown(
                f"**{target[0]}: cached NDMA SACHET response** · "
                f"retrieved {report.get('retrieved_at_utc') or 'time unavailable'}"
            )
            alerts = report.get("alerts", [])
            if not isinstance(alerts, list) or not alerts:
                st.info(
                    "The saved query returned no matching alerts. This cached result "
                    "is not an all-clear and may no longer be current."
                )
            for alert in alerts:
                if not isinstance(alert, dict):
                    continue
                st.warning(
                    f"Historical alert record — current validity unverified: "
                    f"{alert.get('title') or alert.get('hazard_type') or 'Official alert'}"
                )
                st.caption(
                    f"Hazard: {alert.get('hazard_type', 'not supplied')} · "
                    f"Severity: {alert.get('severity', 'not supplied')} · "
                    f"Area: {alert.get('affected_region', 'not supplied')}"
                )
                st.caption(
                    f"Source: {alert.get('issuing_authority', 'NDMA SACHET')} · "
                    f"Expiry: {alert.get('expires_at') or 'not supplied'}"
                )
        return []

    if refresh:
        with st.spinner("Checking NDMA SACHET for selected locations..."):
            stored_reports = refresh_official_alert_reports(
                list(unique_targets.values()),
                force_refresh=True,
            )

    fresh_map_alerts = {}
    affected_route_ids = set()
    known_route_checks = {route_id: [] for route_id in routes_dict}
    displayed_any_response = False

    for coordinate_key, target in unique_targets.items():
        label, latitude, longitude = target[:3]
        entry = stored_reports.get(coordinate_key)
        if not isinstance(entry, dict) or not isinstance(entry.get("last_success"), dict):
            if isinstance(entry, dict) and entry.get("last_error"):
                st.warning(f"{label}: official alert data unavailable. {entry['last_error']}")
            else:
                st.info(f"{label}: official alerts have not been checked yet.")
            continue

        report = entry["last_success"]
        displayed_any_response = True
        stale = report_is_stale(report)
        retrieved_at = report.get("retrieved_at_utc")
        summary = classify_alert_report(report)
        summary_labels = {
            "active": f"{summary['active_count']} active report(s) returned",
            "scheduled": f"{summary['scheduled_count']} scheduled report(s)",
            "expired": f"{summary['expired_count']} expired report(s) only",
            "validity_unknown": (
                f"{summary['validity_unknown_count']} report(s) with unknown validity"
            ),
            "no_matching_alerts": "No matching alerts returned",
            "stale": "Stale response",
            "unavailable": "Unavailable",
        }
        st.metric(f"{label} · NDMA SACHET", summary_labels[summary["status"]])
        if stale:
            st.warning(
                f"{label}: showing the last successful SACHET response as stale "
                f"(retrieved {retrieved_at or 'time unavailable'}). "
                "Its alerts are not used to flag routes."
            )
        elif entry.get("last_error"):
            st.warning(
                f"{label}: the latest refresh failed; showing the earlier successful "
                f"response from {retrieved_at or 'an unknown time'}."
            )
        else:
            st.caption(f"{label}: last successful SACHET update {retrieved_at}.")

        if entry.get("last_error"):
            st.caption(f"Latest refresh error: {entry['last_error']}")
        if report.get("malformed_alert_count"):
            st.warning(
                f"{label}: {report['malformed_alert_count']} malformed alert record(s) "
                "were ignored."
            )

        active_alerts = []
        scheduled_alerts = []
        unknown_validity_alerts = []
        scheduled_count = 0
        expired_count = 0
        for alert in report.get("alerts", []):
            state = alert_time_state(alert)
            if state == "expired":
                expired_count += 1
            elif state == "scheduled":
                scheduled_count += 1
                scheduled_alerts.append(alert)
            elif state == "current":
                active_alerts.append(alert)
            else:
                unknown_validity_alerts.append(alert)

        if expired_count:
            st.caption(f"{label}: {expired_count} expired alert(s) excluded from current route checks.")
        if (
            not active_alerts
            and not scheduled_alerts
            and not unknown_validity_alerts
            and expired_count
        ):
            st.info(
                f"{label}: only expired alerts were returned; they are excluded from "
                "current route checks."
            )
        elif (
            not active_alerts
            and not scheduled_alerts
            and not unknown_validity_alerts
            and not expired_count
            and not stale
        ):
            st.info(
                f"{label}: SACHET returned no matching alert in this location query. "
                "Coverage and completeness are not established; this is not an all-clear."
            )

        for alert in scheduled_alerts:
            if stale:
                continue
            st.info(
                f"{label}: scheduled official alert — "
                f"{alert.get('title') or alert['hazard_type']} · "
                f"source severity: {alert['severity']}."
            )
            st.write(f"**Affected region:** {alert['affected_region']}")
            st.write(f"**Issuing authority:** {alert['issuing_authority']}")
            if alert.get("issue_time"):
                st.caption(f"Issue time: {alert['issue_time']}")
            st.caption(
                f"Effective from: {alert.get('effective_from') or 'not supplied'} · "
                f"Expires: {alert.get('expires_at') or 'not supplied'}"
            )
            st.caption(
                "Coverage and route overlap are not evaluated for a scheduled alert "
                "before its effective time."
            )
            if alert.get("message"):
                st.info(alert["message"])
            if alert.get("alert_url_is_specific"):
                st.markdown(f"[Open original official alert]({alert['official_url']})")
            else:
                st.markdown(f"[Open official SACHET alert listing]({alert['portal_url']})")

        for alert in unknown_validity_alerts:
            st.warning(
                f"{label}: official alert record present, but current validity "
                f"cannot be confirmed — {alert.get('title') or alert['hazard_type']}."
            )
            st.write(f"**Hazard type:** {alert['hazard_type']}")
            st.write(f"**Severity:** {alert['severity']}")
            st.write(f"**Affected area:** {alert['affected_region']}")
            st.write(f"**Issuing authority:** {alert['issuing_authority']}")
            if alert.get("issue_time"):
                st.caption(f"Issue time: {alert['issue_time']}")
            st.caption(
                "Effective time or expiry was not supplied or could not be parsed; "
                "this record is not used to flag routes."
            )
            if alert.get("alert_url_is_specific"):
                st.markdown(f"[Open original official alert]({alert['official_url']})")
            else:
                st.markdown(f"[Open official SACHET alert listing]({alert['portal_url']})")

        for alert in active_alerts:
            coverage = (
                None
                if stale
                else alert_covers_point(alert, latitude, longitude)
            )
            route_intersections = {}
            if not stale:
                for route_id, route in routes_dict.items():
                    intersects = route_intersects_alert(
                        alert, route.get("route_coords")
                    )
                    route_intersections[route_id] = intersects
                    if intersects is not None:
                        known_route_checks[route_id].append(intersects)
                    if intersects is True:
                        affected_route_ids.add(route_id)
                if alert.get("geometry"):
                    fresh_map_alerts.setdefault(alert["id"], alert)

            if stale:
                coverage_text = "Coverage not evaluated because this response is stale."
            elif coverage is True:
                coverage_text = f"Source polygon covers the selected {label.lower()}."
            elif coverage is False:
                coverage_text = (
                    f"Returned within the portal search radius, but its polygon "
                    f"does not cover the selected {label.lower()}."
                )
            else:
                coverage_text = (
                    "Applicability unknown: the source did not provide a usable "
                    "geographic polygon."
                )

            if (
                not stale
                and (
                    coverage is True
                    or any(value is True for value in route_intersections.values())
                )
            ):
                st.warning(
                    f"Official alert: {alert.get('title') or alert['hazard_type']} · "
                    f"source severity: {alert['severity']}"
                    + (
                        f" · source severity level: {alert['severity_level']}"
                        if alert.get("severity_level")
                        else ""
                    )
                )
            else:
                st.markdown(
                    f"**{alert['hazard_type']}** · Source severity: "
                    f"**{alert['severity']}**"
                )
            if alert_time_state(alert) == "time_not_supplied":
                st.caption(
                    "The source supplied no effective or expiry time; current validity "
                    "cannot be confirmed."
                )
            st.write(f"**Affected region:** {alert['affected_region']}")
            st.write(f"**Issuing authority:** {alert['issuing_authority']}")
            st.caption(coverage_text)
            if alert.get("issue_time"):
                st.caption(f"Issue time: {alert['issue_time']}")
            if alert.get("effective_from"):
                st.caption(f"Effective from: {alert['effective_from']}")
            if alert.get("expires_at"):
                st.caption(f"Expires: {alert['expires_at']}")
            if alert.get("message"):
                st.info(alert["message"])

            affected_names = [
                routes_dict[route_id].get("name", route_id)
                for route_id, intersects in route_intersections.items()
                if intersects is True
            ]
            if stale:
                st.caption("Route overlap not evaluated against stale alert data.")
            elif affected_names:
                st.warning(
                    "Returned alert geometry intersects route option(s): "
                    + ", ".join(affected_names)
                    + ". This is an overlap flag, not a route-safety assessment."
                )
            elif any(value is False for value in route_intersections.values()):
                st.caption(
                    "No intersection was found with this alert polygon in the "
                    "available route geometry."
                )
            else:
                st.caption("Route overlap unknown because usable geometry is unavailable.")

            if alert.get("alert_url_is_specific"):
                st.markdown(f"[Open original official alert]({alert['official_url']})")
            else:
                st.markdown(f"[Open official SACHET alert listing]({alert['portal_url']})")
                st.caption("The source did not provide a direct link for this individual alert.")

    if displayed_any_response and not fresh_map_alerts and not affected_route_ids:
        st.caption(
            "Only fresh alerts with source-provided geometries can be used for map "
            "overlays or route-intersection checks."
        )
    if affected_route_ids:
        candidate_ids = [
            route_id
            for route_id in routes_dict
            if route_id not in affected_route_ids
            and known_route_checks.get(route_id)
            and not any(known_route_checks[route_id])
        ]
        if candidate_ids:
            candidate_names = [
                routes_dict[route_id].get("name", route_id)
                for route_id in candidate_ids
            ]
            st.info(
                "Existing route options with no intersection against the returned "
                "alert geometries: "
                + ", ".join(candidate_names)
                + ". These are not verified safe; alert queries cover the selected "
                "locations' 20 km search radii, not necessarily the full route corridor."
            )

    return list(fresh_map_alerts.values())


# -----------------------------------------------------------------------------
# 4. Main Application
# -----------------------------------------------------------------------------
def render_data_status_summary():
    """Render a compact summary of actual loaded data without implying live monitoring."""
    pills = []
    if st.session_state.get("aegis_offline_mode"):
        pills.append("OFFLINE MODE")
    else:
        pills.append("Online services enabled")
    if st.session_state.get("resolved_route_locations"):
        pills.append("Route loaded")
    else:
        pills.append("Awaiting route")

    weather_reports = st.session_state.get("weather_reports", {})
    if weather_reports:
        pills.append(f"Weather: {len(weather_reports)} check(s)")
    else:
        pills.append("Weather: not loaded")

    alert_reports = st.session_state.get("official_alert_reports", {})
    if alert_reports:
        pills.append(f"Alerts: {len(alert_reports)} source(s)")
    else:
        pills.append("Alerts: not checked")

    if st.session_state.get("active_emergency_alerts"):
        pills.append("Early-warning active")
    elif st.session_state.get("emergency_monitor_last_refresh_utc"):
        pills.append("Early-warning refreshed")
    else:
        pills.append("Early-warning: unavailable")

    pills_html = "".join(
        f'<span class="aegis-data-pill">{html.escape(item)}</span>' for item in pills
    )
    st.markdown(
        f'<div class="aegis-status-row">{pills_html}</div>',
        unsafe_allow_html=True,
    )


def render_safety_disclaimer():
    """Keep prototype safety guidance visible on both empty and populated views."""
    st.markdown(
        '<section class="aegis-safety-notice" aria-label="Safety and prototype notice">'
        "<strong>🛡️ Safety &amp; Prototype Notice</strong>"
        "<p>AEGIS is an educational prototype. Routes and forecast indicators do not guarantee "
        "safety or predict every emergency. Verify hazards, road conditions and shelters through "
        "official sources. <strong>Unknown does not mean safe.</strong> Always follow official "
        "emergency instructions.</p>"
        "</section>",
        unsafe_allow_html=True,
    )
    with st.expander("Read Full Safety Disclaimer"):
        st.markdown(
            "This software is an educational algorithmic demonstration. Routes are calculated "
            "based on spatial distance and simulated parameters. A route is **NOT** certified safe "
            "merely because it is shortest or bypasses a user-defined hazard. Shelter locations and "
            "road availability must be verified through official local emergency management "
            "authorities during a real disaster. Voice guidance is an experimental browser feature, "
            "not certified real-world emergency navigation; it does not provide verified safety, "
            "live traffic, or independently verified hazard detection. The early-warning panel "
            "shows forecast indicators only: DHM official warnings and locally validated flood "
            "thresholds are not connected, and **Unknown does not mean safe**. Use official "
            "emergency instructions during real incidents."
        )


def load_styles():
    """Load presentation-only dashboard refinements from the adjacent stylesheet."""
    stylesheet_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "styles.css")
    with open(stylesheet_path, encoding="utf-8") as stylesheet:
        st.markdown(f"<style>{stylesheet.read()}</style>", unsafe_allow_html=True)


def main():
    load_styles()
    st.markdown(
        """
        <header class="aegis-header">
          <span class="aegis-icon" aria-hidden="true">
            <svg viewBox="0 0 32 36" fill="none" xmlns="http://www.w3.org/2000/svg">
              <path d="M16 2.5 28 7v9.4c0 8.2-5.6 13.6-12 17.1C9.6 30 4 24.6 4 16.4V7l12-4.5Z" stroke="currentColor" stroke-width="2.2" stroke-linejoin="round"/>
              <path d="m19.9 11.7-2.1 7.1-6.1 3.4 2.1-7.1 6.1-3.4Z" fill="currentColor"/>
            </svg>
          </span>
          <div class="aegis-brand">AEGIS</div>
          <div class="aegis-header-copy">
            <strong>Emergency Guidance &amp; Intelligent Safety</strong>
            <span>Situational route planning, advisories, and emergency review</span>
          </div>
        </header>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(
        "Compare mapped road options or explore the A–F algorithm demo. Route and hazard information is advisory and unverified."
    )

    # Sidebar Navigation Mode
    st.sidebar.header("🧭 Operating Mode")
    mode = st.sidebar.radio(
        "Select Mode:",
        options=[
            "🌍 Real-World Multi-Route Dashboard (OpenStreetMap)",
            "🔬 Simulated Graph Demo (Academic A–F & K-Shortest Paths)",
        ],
        index=0,
    )
    first_page_active = mode == "🌍 Real-World Multi-Route Dashboard (OpenStreetMap)"
    if first_page_active:
        st.markdown(
            '<div class="aegis-first-page-scope" aria-hidden="true"></div>',
            unsafe_allow_html=True,
        )
    offline_mode = st.sidebar.checkbox(
        "Use Offline Mode",
        value=False,
        key="aegis_offline_mode",
        help=(
            "Disable all external geocoding, routing, weather, and alert requests. "
            "Offline real-world routes require an imported local GeoJSON road dataset."
        ),
    )
    render_data_status_summary()
    local_summary = database_summary()
    st.sidebar.caption(
        f"Local data: {local_summary['road_segments']} road segments · "
        f"{local_summary['places']} places · "
        f"{local_summary['shelters']} shelters"
    )
    if local_summary.get("coverage_bounds"):
        coverage = local_summary["coverage_bounds"]
        st.sidebar.caption(
            "Road-data coordinate bounds: "
            f"{coverage['min_latitude']:.4f}–{coverage['max_latitude']:.4f}° lat, "
            f"{coverage['min_longitude']:.4f}–{coverage['max_longitude']:.4f}° lon. "
            "Bounds do not guarantee continuous road coverage."
        )
    local_geojson = st.sidebar.file_uploader(
        "Import offline GeoJSON",
        type=["geojson", "json"],
        key="aegis_offline_geojson",
        help=(
            "Imports local LineString/MultiLineString roads and named Point places. "
            "Shelter points may set category='shelter'."
        ),
    )
    if st.sidebar.button(
        "Save local geographic data",
        key="import_aegis_offline_geojson",
        disabled=local_geojson is None,
    ):
        try:
            imported = import_geojson(
                local_geojson.getvalue().decode("utf-8"),
                local_geojson.name,
            )
        except (UnicodeDecodeError, ValueError) as error:
            st.sidebar.error(f"Local dataset was not imported: {error}")
        else:
            st.sidebar.success(
                f"Imported {imported['road_segments']} road segments, "
                f"{imported['places']} places, and {imported['shelters']} shelters."
            )
    st.sidebar.markdown("---")

    # =========================================================================
    # MODE 1: REAL-WORLD MULTI-ROUTE DASHBOARD
    # =========================================================================
    if first_page_active:
        st.subheader("Plan a route")
        with st.container(border=True):
            st.markdown("#### Starting point and destination")
            location_event = _browser_location_component(
                key="browser_location_picker",
                default=None,
            )
            if (
                isinstance(location_event, dict)
                and location_event.get("request_id")
                != st.session_state.get("last_browser_location_request_id")
            ):
                st.session_state["last_browser_location_request_id"] = location_event.get("request_id")
                if location_event.get("status") == "success":
                    try:
                        latitude = float(location_event.get("latitude"))
                        longitude = float(location_event.get("longitude"))
                        accuracy = float(location_event.get("accuracy"))
                        valid_location = (
                            math.isfinite(latitude)
                            and math.isfinite(longitude)
                            and math.isfinite(accuracy)
                            and -90 <= latitude <= 90
                            and -180 <= longitude <= 180
                            and accuracy >= 0
                        )
                    except (TypeError, ValueError):
                        valid_location = False

                    if valid_location:
                        st.session_state["browser_location_result"] = {
                            "status": "success",
                            "latitude": latitude,
                            "longitude": longitude,
                            "accuracy": accuracy,
                            "timestamp": location_event.get("timestamp"),
                        }
                        st.session_state["starting_point_mode"] = "My current location"
                    else:
                        st.session_state["browser_location_result"] = {
                            "status": "error",
                            "message": "The browser returned invalid coordinates. Use the manual starting point.",
                        }
                else:
                    st.session_state["browser_location_result"] = {
                        "status": location_event.get("status", "error"),
                        "message": location_event.get(
                            "message",
                            "Location could not be determined. Use the manual starting point.",
                        ),
                    }

            starting_point_mode = st.radio(
                "Starting point source:",
                options=["Manual address / place", "My current location"],
                key="starting_point_mode",
                horizontal=True,
                help="Live location is requested only when you press Use My Current Location.",
            )
            location_result = st.session_state.get("browser_location_result")
            if isinstance(location_result, dict):
                if location_result.get("status") == "success":
                    st.info(
                        "Location detected: "
                        f"`{location_result['latitude']:.6f}, {location_result['longitude']:.6f}` "
                        f"· Browser-reported accuracy ±{location_result['accuracy']:.0f} m"
                    )
                else:
                    st.warning(location_result.get("message", "Location unavailable."))
            st.caption(
                (
                    "Offline mode searches only names imported into this device; "
                    "browser coordinates are retained without reverse lookup."
                    if offline_mode
                    else "Reverse lookup may send selected coordinates to OpenStreetMap Nominatim. "
                    "Manual address entry remains available."
                )
            )
            start_col, destination_col = st.columns(2)
            with start_col:
                start_addr = st.text_input(
                    "Manual starting address or place",
                    value="Connaught Place, New Delhi",
                    help="Used when Manual address / place is selected.",
                    key="manual_start_address",
                )
            with destination_col:
                dest_addr = st.text_input(
                    "Destination or shelter",
                    value="India Gate, New Delhi",
                    help="Enter a place name or full address, such as a station, hospital, or school.",
                    key="route_destination_address",
                )
                st.caption(
                    (
                        "Place lookup: imported local GeoJSON only. No network lookup is attempted."
                        if offline_mode
                        else "Place lookup: OpenStreetMap Nominatim · © OpenStreetMap contributors. "
                        "Lookup runs only when you request a route."
                    )
                )
            plan_requested = st.button(
                "Generate Evacuation Plan",
                type="primary",
                use_container_width=True,
            )
            include_destination_weather = st.checkbox(
                "Include destination weather",
                value=False,
                help="When requested, fetch a separate forecast for the destination coordinates.",
                key="include_destination_weather",
            )
            weather_requested = st.button(
                "Get weather for selected location(s)",
                key="get_selected_location_weather",
                help="Fetch current and hourly weather from Open-Meteo. Reports are cached for 15 minutes.",
                disabled=offline_mode,
            )

        st.sidebar.markdown("---")
        st.sidebar.subheader("Candidate Shelter Evaluation (Optional)")
        enable_shelter = st.sidebar.checkbox(
            "Evaluate Candidate Shelter",
            value=True,
            help="Compare your primary destination against an alternative emergency shelter to find the nearest reachable shelter by road distance.",
        )
        shelter_addr = ""
        if enable_shelter:
            shelter_addr = st.sidebar.text_input(
                "🛡️ Alternative Candidate Shelter:",
                value="Major Dhyan Chand National Stadium, New Delhi",
                help="Type an alternative candidate evacuation shelter (labeled as user-provided, unverified).",
            )

        st.sidebar.markdown("---")
        st.sidebar.subheader("Simulated Hazard & Road Closures")
        enable_hazard = st.sidebar.checkbox(
            "Simulate Road Hazard / Closure",
            value=True,
            help="Specify a hazard zone (flood, fire, structural debris) to test dynamic hazard-avoiding rerouting.",
        )
        hazard_addr = ""
        hazard_radius_m = 400
        hazard_desc = "Simulated Road Closure / Flood Obstruction"

        if enable_hazard:
            hazard_addr = st.sidebar.text_input(
                "⚠️ Hazard Location / Street:",
                value="Janpath, New Delhi",
                help="Address or intersection of the road hazard.",
            )
            hazard_radius_m = st.sidebar.slider(
                "Hazard Danger Buffer (Meters):",
                min_value=150,
                max_value=1500,
                value=400,
                step=50,
                help="Radius around the hazard location considered impassable or dangerous.",
            )
            hazard_desc = st.sidebar.text_input(
                "Hazard Description:",
                value="Simulated Flood Obstruction / Debris",
            )
            st.sidebar.caption(
                f"ℹ️ Hazard Metadata: Source: Simulated User Input | Recorded: {datetime.now().strftime('%Y-%m-%d %H:%M')} | Status: Unverified"
            )

        st.sidebar.markdown("---")
        st.sidebar.subheader("Map Highlighting")
        highlight_option = st.sidebar.selectbox(
            "Highlight Route on Map:",
            options=[
                "All Routes (Overview)",
                "Primary Route (Shortest Distance)",
                "Alternative Route 1 (Lateral Corridor A)",
                "Alternative Route 2 (Lateral Corridor B)",
                "Hazard-Bypassing Route",
                "Nearest Reachable Shelter",
            ],
            index=0,
            help="Select an individual route to emphasize and use for voice navigation. Overview mode uses the primary route for guidance.",
        )

        # Resolve addresses only after an explicit route-planning request.
        if not dest_addr.strip():
            st.error("Please provide a primary destination.")
            render_safety_disclaimer()
            return

        if starting_point_mode == "My current location":
            if not isinstance(location_result, dict) or location_result.get("status") != "success":
                st.error(
                    "No current location is available. Press **Use My Current Location** "
                    "and allow browser access, or select **Manual address / place**."
                )
                render_safety_disclaimer()
                return
            start_signature = (
                "browser_geolocation",
                location_result["latitude"],
                location_result["longitude"],
                location_result["accuracy"],
            )
        else:
            if not start_addr.strip():
                st.error("Please enter a manual starting address or select My current location.")
                render_safety_disclaimer()
                return
            start_signature = ("manual_address", start_addr.strip())
        destination_signature = ("address", dest_addr.strip())
        resolution_signature = (
            offline_mode,
            start_signature,
            destination_signature,
        )
        resolved_locations = st.session_state.get("resolved_route_locations")

        if plan_requested:
            if starting_point_mode == "My current location":
                s_lat = location_result["latitude"]
                s_lon = location_result["longitude"]
                start_accuracy_m = location_result["accuracy"]
                if offline_mode:
                    s_name = f"Detected coordinates ({s_lat:.6f}, {s_lon:.6f})"
                else:
                    with st.spinner("Looking up the detected coordinates with OpenStreetMap Nominatim..."):
                        s_name, reverse_error = cached_reverse_geocode(s_lat, s_lon)
                    if not s_name:
                        s_name = f"Detected coordinates ({s_lat:.6f}, {s_lon:.6f})"
                        st.info(f"{reverse_error} The route will use the detected coordinates.")
                start_source = "Browser geolocation"
            else:
                if offline_mode:
                    local_match = search_local_places(start_addr)
                    if local_match["success"]:
                        place = local_match["place"]
                        s_lat, s_lon, s_name = (
                            place["latitude"],
                            place["longitude"],
                            place["name"],
                        )
                    else:
                        s_lat, s_lon, s_name = None, None, local_match["error"]
                else:
                    with st.spinner("Geocoding the starting point via OpenStreetMap Nominatim..."):
                        s_lat, s_lon, s_name = cached_geocode_address(start_addr)
                if s_lat is None:
                    st.error(f"❌ **Origin Geocoding Error**: {s_name}")
                    st.info(
                        "Correct the starting place and press **Generate Evacuation Plan** to retry."
                    )
                    render_safety_disclaimer()
                    return
                start_accuracy_m = None
                start_source = (
                    "Local saved place"
                    if offline_mode
                    else "Manual address/place"
                )

            if offline_mode:
                local_match = search_local_places(dest_addr)
                if local_match["success"]:
                    place = local_match["place"]
                    d_lat, d_lon, d_name = (
                        place["latitude"],
                        place["longitude"],
                        place["name"],
                    )
                else:
                    d_lat, d_lon, d_name = None, None, local_match["error"]
            else:
                with st.spinner("Finding the destination with OpenStreetMap Nominatim..."):
                    d_lat, d_lon, d_name = cached_geocode_address(dest_addr)
            if d_lat is None:
                st.error(f"Destination lookup failed: {d_name}")
                st.caption(
                    "No alternate destination was selected. You can edit the place name and press "
                    "**Generate Evacuation Plan** to retry."
                )
                render_safety_disclaimer()
                return

            st.session_state["selected_start_location"] = {
                "source": "browser_geolocation" if starting_point_mode == "My current location" else "manual_address",
                "query": start_addr.strip() if starting_point_mode != "My current location" else None,
                "latitude": s_lat,
                "longitude": s_lon,
                "accuracy_m": start_accuracy_m,
                "display_name": s_name,
            }
            resolved_locations = {
                "signature": resolution_signature,
                "start": (s_lat, s_lon, s_name, start_source, start_accuracy_m),
                "destination": (d_lat, d_lon, d_name),
                "offline_mode": offline_mode,
            }
            st.session_state["resolved_route_locations"] = resolved_locations

        if (
            not isinstance(resolved_locations, dict)
            or resolved_locations.get("signature") != resolution_signature
        ):
            st.info("Press **Generate Evacuation Plan** to look up the selected locations.")
            render_safety_disclaimer()
            return

        s_lat, s_lon, s_name, start_source, start_accuracy_m = resolved_locations["start"]
        d_lat, d_lon, d_name = resolved_locations["destination"]

        if d_lat is None:
            st.error(f"❌ **Destination Geocoding Error**: {d_name}")
            render_safety_disclaimer()
            return
        st.info(
            f"**Starting point ({start_source}):** {s_name} — "
            f"`{s_lat:.6f}, {s_lon:.6f}`"
            + (
                f" (browser-reported accuracy ±{start_accuracy_m:.0f} m)"
                if start_accuracy_m is not None
                else ""
            )
            + f"\n\n**Destination:** {d_name}"
        )

        st.subheader("Emergency overview")
        st.caption(
            "Forecasts, official alerts, and route data are shown separately with source and freshness context."
        )
        st.markdown("#### Current weather forecast")
        st.caption(
            "Forecast data is separate from route calculations and is not a safety assessment. "
            "Weather and flood forecasts are checked when a new route location is selected; "
            "use the refresh controls to request updated data."
        )
        weather_targets = [
            ("Starting location", s_lat, s_lon, s_name),
        ]
        if include_destination_weather:
            weather_targets.append(("Destination", d_lat, d_lon, d_name))

        monitor_reports = st.session_state.get("emergency_monitor_reports", {})
        targets_need_forecasts = False
        for _, latitude, longitude, _ in weather_targets:
            coordinate_key = f"{latitude:.6f},{longitude:.6f}"
            entry = (
                monitor_reports.get(coordinate_key)
                if isinstance(monitor_reports, dict)
                else None
            )
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("weather"), dict)
                or not isinstance(entry.get("flood"), dict)
            ):
                targets_need_forecasts = True
                break
        if targets_need_forecasts and not offline_mode:
            with st.spinner("Retrieving weather and flood forecasts for the selected location..."):
                refresh_forecast_reports(weather_targets)
        elif offline_mode:
            saved_weather = st.session_state.setdefault("weather_reports", {})
            for label, latitude, longitude, location_name in weather_targets:
                coordinate_key = f"{latitude:.6f},{longitude:.6f}"
                if coordinate_key not in saved_weather:
                    cached_report = load_cached_report("weather", latitude, longitude)
                    if cached_report:
                        saved_weather[coordinate_key] = {
                            "location_name": location_name,
                            "report": cached_report,
                        }
            st.session_state["weather_reports"] = saved_weather

        if weather_requested and not offline_mode:
            cached_weather_forecast.clear()
            reports = st.session_state.setdefault("weather_reports", {})
            with st.spinner("Retrieving weather from Open-Meteo..."):
                for label, latitude, longitude, location_name in weather_targets:
                    coordinate_key = f"{latitude:.6f},{longitude:.6f}"
                    report = cached_weather_forecast(latitude, longitude)
                    reports[coordinate_key] = {
                        "location_name": location_name,
                        "report": report,
                    }
                    monitor_reports = st.session_state.get(
                        "emergency_monitor_reports",
                        {},
                    )
                    if isinstance(monitor_reports, dict):
                        monitor_entry = monitor_reports.get(coordinate_key)
                        if isinstance(monitor_entry, dict):
                            monitor_entry["weather"] = report
                            monitor_entry["weather_last_success"] = last_successful_report(
                                report,
                                monitor_entry.get("weather_last_success"),
                            )
            st.session_state["weather_reports"] = reports

        saved_reports = st.session_state.get("weather_reports", {})
        displayed_reports = 0
        for label, latitude, longitude, location_name in weather_targets:
            coordinate_key = f"{latitude:.6f},{longitude:.6f}"
            saved_entry = saved_reports.get(coordinate_key)
            if isinstance(saved_entry, dict) and isinstance(saved_entry.get("report"), dict):
                render_weather_report(label, saved_entry["report"])
                displayed_reports += 1

        if displayed_reports < len(weather_targets):
            st.info(
                "Weather has not been loaded for every selected location. "
                "Press **Get weather for selected location(s)** to request it."
            )

        st.subheader("Flood forecast and early-warning monitor")
        st.caption(
            "This advisory panel evaluates only the explicitly selected real-world locations. "
            "It does not monitor or modify the simulated A–F demo."
        )
        monitor_controls = st.columns(3)
        with monitor_controls[0]:
            monitor_destination = st.checkbox(
                "Also monitor destination",
                value=False,
                key="monitor_emergency_destination",
            )
        with monitor_controls[1]:
            refresh_option = st.selectbox(
                "Automatic refresh",
                options=["Off", "15 minutes", "30 minutes", "60 minutes"],
                index=0,
                key="emergency_monitor_refresh_interval",
                help=(
                    "When enabled, selected-location forecasts are checked at this interval. "
                    "A user-triggered refresh is always available."
                ),
            )
        with monitor_controls[2]:
            rainfall_threshold = st.number_input(
                "Rainfall screening trigger (mm/hour)",
                min_value=1.0,
                max_value=100.0,
                value=DEFAULT_RAINFALL_SCREENING_THRESHOLD_MM,
                step=1.0,
                key="emergency_rainfall_screening_threshold",
                help=(
                    "AEGIS screening value only, not a DHM/local authority threshold "
                    "or a validated flash-flood threshold."
                ),
            )

        monitor_interval_minutes = {
            "Off": 0,
            "15 minutes": 15,
            "30 minutes": 30,
            "60 minutes": 60,
        }[refresh_option]
        monitor_targets = [
            ("Starting location", s_lat, s_lon, s_name),
        ]
        if monitor_destination:
            monitor_targets.append(("Destination", d_lat, d_lon, d_name))
        official_alert_targets = [
            ("Starting location", s_lat, s_lon, s_name),
            ("Destination", d_lat, d_lon, d_name),
        ]
        render_emergency_monitoring_panel(
            monitor_targets,
            monitor_interval_minutes,
            rainfall_threshold,
            official_alert_targets,
            offline_mode,
        )

        # Resolve optional hazard from local records while offline.
        hazard_info = None
        if enable_hazard and hazard_addr.strip():
            if offline_mode:
                hazard_match = search_local_places(hazard_addr)
                if hazard_match["success"]:
                    hazard_place = hazard_match["place"]
                    h_lat, h_lon, h_name = (
                        hazard_place["latitude"],
                        hazard_place["longitude"],
                        hazard_place["name"],
                    )
                else:
                    h_lat, h_lon, h_name = None, None, hazard_match["error"]
            else:
                with st.spinner("Geocoding hazard location..."):
                    h_lat, h_lon, h_name = cached_geocode_address(hazard_addr)
            if h_lat is not None:
                hazard_info = {
                    "lat": h_lat,
                    "lon": h_lon,
                    "radius_km": hazard_radius_m / 1000.0,
                    "description": hazard_desc,
                    "name": h_name,
                    "source": (
                        hazard_place.get("source")
                        if offline_mode and "hazard_place" in locals()
                        else "Simulated user input; unverified"
                    ),
                    "category": (
                        hazard_place.get("category")
                        if offline_mode and "hazard_place" in locals()
                        else "simulated"
                    ),
                }
            else:
                st.warning(
                    f"Could not locate hazard address '{hazard_addr}'. "
                    f"{h_name} Hazard avoidance will be skipped."
                )

        # Resolve optional shelter from local records while offline.
        shelter_info = None
        if enable_shelter and shelter_addr.strip():
            if offline_mode:
                shelter_match = search_local_places(shelter_addr, category="shelter")
                if shelter_match["success"]:
                    shelter_place = shelter_match["place"]
                    sh_lat, sh_lon, sh_name = (
                        shelter_place["latitude"],
                        shelter_place["longitude"],
                        shelter_place["name"],
                    )
                else:
                    sh_lat, sh_lon, sh_name = None, None, shelter_match["error"]
            else:
                with st.spinner("Geocoding candidate shelter..."):
                    sh_lat, sh_lon, sh_name = cached_geocode_address(shelter_addr)
            if sh_lat is not None:
                shelter_info = {"name": sh_name, "lat": sh_lat, "lon": sh_lon}
            else:
                st.warning(
                    f"Could not locate candidate shelter '{shelter_addr}'. "
                    f"{sh_name} Nearest-shelter evaluation skipped."
                )

        # Fetch Multi-Route Suite
        with st.spinner(
            "Computing routes from the installed local road graph..."
            if offline_mode
            else "Computing multiple distinct evacuation routes along real road networks..."
        ):
            hazard_tuple = (
                (hazard_info["lat"], hazard_info["lon"], hazard_info["radius_km"], hazard_info["description"])
                if hazard_info
                else None
            )
            shelter_tuple = (
                (shelter_info["name"], shelter_info["lat"], shelter_info["lon"])
                if shelter_info
                else None
            )

            if offline_mode:
                routes_dict = generate_local_routes(s_lat, s_lon, d_lat, d_lon)
                if "error" not in routes_dict and hazard_info:
                    for route in routes_dict.values():
                        intersects, minimum_distance = check_route_hazard_proximity(
                            route.get("route_coords", []),
                            hazard_info["lat"],
                            hazard_info["lon"],
                            hazard_info["radius_km"],
                        )
                        route["hazard_status"] = (
                            (
                                f"Route geometry passes within {minimum_distance * 1000:.0f} m "
                                "of the selected hazard point."
                            )
                            if intersects
                            else "No route segment was found within the selected hazard buffer; this is not a safety clearance."
                        )
                        route["hazard_source"] = hazard_info.get(
                            "source",
                            "Simulated user input; unverified",
                        )
                        if hazard_info.get("category") == "hazard":
                            route["hazard_status"] += (
                                " Imported hazard validity is unknown; no timestamp "
                                "or official verification was supplied."
                            )
                if "error" not in routes_dict and shelter_info:
                    shelter_routes = generate_local_routes(
                        s_lat,
                        s_lon,
                        shelter_info["lat"],
                        shelter_info["lon"],
                    )
                    shelter_route = shelter_routes.get("primary")
                    if shelter_route:
                        shelter_route = dict(shelter_route)
                        shelter_route.update({
                            "id": "nearest_shelter",
                            "name": f"Local route to candidate shelter: {shelter_info['name']}",
                            "color": "#319795",
                            "rationale": (
                                "Shortest path in the installed local road graph to a "
                                "user-provided candidate shelter. Shelter authorization "
                                "and availability are unverified."
                            ),
                        })
                        routes_dict["nearest_shelter"] = shelter_route
                    else:
                        st.warning(
                            "The candidate shelter is in the local place index, but no "
                            "local road route was found to it."
                        )
            else:
                routes_dict = cached_multi_routes(
                    s_lat, s_lon, d_lat, d_lon, hazard_tuple, shelter_tuple
                )

        if not routes_dict or "error" in routes_dict:
            st.error(f"❌ Routing Service Error: {routes_dict.get('error', 'No drivable route found.')}")
            render_safety_disclaimer()
            return
        if plan_requested:
            st.success(
                (
                    "Local route options generated from the imported road dataset. "
                    "Road completeness, current access, and route safety are not verified."
                    if offline_mode
                    else "Route options generated for the selected locations. Current road conditions and route safety are not verified."
                )
            )

        official_map_alerts = render_official_alerts_panel(
            official_alert_targets,
            routes_dict,
            offline_mode,
        )
        routes_intersecting_official_alerts = {
            route_id
            for route_id, route in routes_dict.items()
            if any(
                route_intersects_alert(alert, route.get("route_coords")) is True
                for alert in official_map_alerts
            )
        }

        # Determine route ID from highlight selectbox
        highlight_id_map = {
            "All Routes (Overview)": "all",
            "Primary Route (Shortest Distance)": "primary",
            "Alternative Route 1 (Lateral Corridor A)": "alt_1",
            "Alternative Route 2 (Lateral Corridor B)": "alt_2",
            "Hazard-Bypassing Route": "hazard_avoid",
            "Nearest Reachable Shelter": "nearest_shelter",
        }
        active_highlight_id = highlight_id_map.get(highlight_option, "all")
        selected_route_id = (
            active_highlight_id
            if active_highlight_id in routes_dict
            else "primary"
        )

        # Top Metric Cards
        prim = routes_dict.get("primary", {})
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Shortest Distance", f"{prim.get('distance_km', 'N/A')} km", "Primary Route")
        with col2:
            travel_time = prim.get("duration_mins")
            st.metric(
                "Estimated Drive Time",
                f"{travel_time} min" if travel_time is not None else "Unavailable",
                "No local speed model" if offline_mode else "OpenStreetMap Speed Model",
            )
        with col3:
            st.metric("Candidate Routes", f"{len(routes_dict)} Generated", "Distinct corridors")
        with col4:
            if "hazard_avoid" in routes_dict:
                st.metric("Hazard Status", "Avoidance Active 🛡️", f"{hazard_radius_m}m Buffer")
            else:
                st.metric("Hazard Status", "Unknown", "No verified closure data")

        # Tabs: Map, Turn-by-Turn, Hazard Details
        tab_map, tab_turns, tab_safety = st.tabs([
            "🗺️ Multi-Route Evacuation Map",
            "📋 Turn-by-Turn Navigation Steps",
            "🛡️ Hazard & Shelter Safety Report",
        ])

        with tab_map:
            # Clean, compact multi-route legend
            render_multi_route_legend(
                routes_dict=routes_dict,
                hazard_info=hazard_info,
                shelter_info=shelter_info,
            )

            selected_route_obj = routes_dict[selected_route_id]
            st.caption(
                (
                    f"Navigation route: **{selected_route_obj['name']}**. "
                    "Local instructions are approximate and shown below; this offline map "
                    "does not use remote tiles or GPS voice navigation."
                    if offline_mode
                    else (
                        f"Navigation route: **{selected_route_obj['name']}**. "
                        "Selecting a route in the sidebar also selects it for voice navigation."
                    )
                )
            )
            if offline_mode:
                st.markdown(
                    render_local_map_svg(
                        local_road_segments(),
                        routes_dict,
                        (s_lat, s_lon),
                        (d_lat, d_lon),
                        s_name,
                        d_name,
                        markers=[
                            marker
                            for marker in (
                                {
                                    "latitude": hazard_info["lat"],
                                    "longitude": hazard_info["lon"],
                                    "name": hazard_info["description"],
                                    "category": "hazard",
                                } if hazard_info else None,
                                {
                                    "latitude": shelter_info["lat"],
                                    "longitude": shelter_info["lon"],
                                    "name": shelter_info["name"],
                                    "category": "shelter",
                                } if shelter_info else None,
                            )
                            if marker is not None
                        ],
                    ),
                    unsafe_allow_html=True,
                )
            else:
                folium_multi = build_multi_route_map(
                    start_lat=s_lat,
                    start_lon=s_lon,
                    start_name=s_name,
                    dest_lat=d_lat,
                    dest_lon=d_lon,
                    dest_name=d_name,
                    routes_dict=routes_dict,
                    highlight_route_id=(
                        selected_route_id
                        if active_highlight_id == "all"
                        else active_highlight_id
                    ),
                    hazard_info=hazard_info,
                    shelter_info=shelter_info,
                    start_accuracy_m=start_accuracy_m,
                    official_alerts=official_map_alerts,
                )
                nav_instructions = selected_route_obj.get("steps") or [{
                    "instruction": "Follow the highlighted mapped route to the destination.",
                    "maneuver_location": None,
                }]
                route_signature = hashlib.sha256(
                    json.dumps(
                        selected_route_obj.get("route_coords", []),
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()[:12]
                add_navigation_overlay(
                    folium_multi,
                    instructions=[
                        {
                            "instruction": step["instruction"],
                            "location": step.get("maneuver_location"),
                        }
                        for step in nav_instructions
                    ],
                    route_coords=selected_route_obj.get("route_coords", []),
                    navigation_key=(
                        f"real:{selected_route_id}:{s_lat:.5f}:{s_lon:.5f}:"
                        f"{d_lat:.5f}:{d_lon:.5f}:{selected_route_obj.get('distance_km')}:"
                        f"{len(nav_instructions)}:{route_signature}"
                    ),
                    allow_gps=True,
                )

        with tab_turns:
            st.subheader("Turn-by-Turn Navigation Guide")
            inspect_route_name = st.selectbox(
                "Select Route to Inspect Steps:",
                options=[r["name"] for r in routes_dict.values()],
            )
            selected_route_obj = next((r for r in routes_dict.values() if r["name"] == inspect_route_name), None)

            if selected_route_obj and selected_route_obj.get("steps"):
                st.markdown(f"**Total Navigation Steps**: `{len(selected_route_obj['steps'])} turns`")
                for idx, step in enumerate(selected_route_obj["steps"]):
                    st.write(f"**Step {idx + 1}**: {step['instruction']} &nbsp;·&nbsp; *({step['distance_m']:.0f} m)*")
                st.success(f"🏁 Arrive at destination via **{selected_route_obj['name']}**.")
            else:
                st.info("No step-by-step turns available for this route.")

        with tab_safety:
            st.subheader("Hazard Avoidance & Verification Report")
            col_h1, col_h2 = st.columns(2)

            with col_h1:
                st.markdown("#### ⚠️ Hazard Status & Verification")
                if hazard_info:
                    st.write(f"**Simulated Hazard Zone**: `{hazard_info['description']}`")
                    st.write(f"**Coordinates**: Lat `{hazard_info['lat']:.5f}`, Lon `{hazard_info['lon']:.5f}`")
                    st.write(f"**Safety Buffer Radius**: `{hazard_info['radius_km']*1000:.0f} meters`")
                    st.write(
                        f"**Data Source**: `{hazard_info.get('source', 'Simulated user input; unverified')}`"
                    )
                    st.warning(
                        "Hazard avoidance uses point-to-route proximity only. Imported "
                        "local hazard records have unknown freshness and official status."
                        if offline_mode
                        else "All hazard avoidance calculations are based purely on synthetic coordinates and great-circle buffer proximity."
                    )
                else:
                    st.info("No simulated hazard zone defined. All routes evaluated on open road geometry.")

            with col_h2:
                st.markdown("#### 🛡️ Shelter Authorization Notice")
                if shelter_info:
                    st.write(f"**Candidate Shelter**: `{shelter_info['name']}`")
                    st.write(f"**Coordinates**: Lat `{shelter_info['lat']:.5f}`, Lon `{shelter_info['lon']:.5f}`")
                    st.write(f"**Authorization Status**: `User-provided (Unverified)`")
                    st.info("Do not assume public buildings, stadiums, or schools are authorized emergency evacuation centers without official municipal civil defense verification.")
                else:
                    st.write("No candidate shelters entered.")

        st.subheader("Available routes")
        st.caption(
            (
                "Local road routes from imported GeoJSON. Travel times are unavailable "
                "unless the dataset supplies a speed model; road access and local hazard "
                "status remain unverified."
                if offline_mode
                else "Real-world road routes from OpenStreetMap/OSRM. Travel times are provider estimates; "
                "hazard checks use simulated, user-entered information."
            )
        )
        render_route_cards([
            {
                "title": route["name"],
                "category": route.get(
                    "source",
                    "OSRM · OpenStreetMap road data",
                ),
                "accent": route.get("color", "#0f766e"),
                "selected": route_id == selected_route_id,
                "metrics": [
                    ("Road distance", f"{route['distance_km']} km"),
                    (
                        "Travel time",
                        (
                            f"{route['duration_mins']} min"
                            if route.get("duration_mins") is not None
                            else "Unavailable"
                        ),
                    ),
                ],
                "status": (
                    (
                        "Intersects returned SACHET alert geometry; overlap is not a safety assessment."
                        if route_id in routes_intersecting_official_alerts
                        else "No intersection with the returned SACHET alert geometries; this does not establish safety."
                    )
                    + " "
                    + (
                        route["hazard_status"]
                        if hazard_info
                        else "No user-provided hazard data; hazard proximity was not evaluated."
                    )
                ),
                "details": [
                    "Official alert coverage is checked only against source-provided polygons returned for the selected locations; absent intersection is not safety clearance.",
                    "Hazard checks use user-entered, unverified data; they are not live hazard reports.",
                    route["safety_disclaimer"],
                ],
            }
            for route_id, route in routes_dict.items()
        ])

        # Retain the full comparison details in a compact disclosure.
        table_rows = []
        for route in routes_dict.values():
            color_badge = {
                "#2f855a": "🟢 Green",
                "#3182ce": "🔵 Blue",
                "#805ad5": "🟣 Purple",
                "#dd6b20": "🟠 Coral",
                "#319795": "🔷 Teal",
            }.get(route["color"], route["color"])

            table_rows.append({
                "Route Option": route["name"],
                "Map Color": color_badge,
                "Distance (km)": f"{route['distance_km']} km",
                "Travel Time": (
                    f"{route['duration_mins']} mins"
                    if route.get("duration_mins") is not None
                    else "Unavailable"
                ),
                "Hazard Evaluation": (
                    route["hazard_status"]
                    if hazard_info
                    else "No user-provided hazard data; not evaluated."
                ),
                "Recommendation Rationale": route["rationale"],
                "Safety Status": route["safety_disclaimer"],
            })

        with st.expander("Detailed route comparison"):
            st.dataframe(table_rows, use_container_width=True)

    # =========================================================================
    # MODE 2: SIMULATED GRAPH & K-SHORTEST PATHS (ACADEMIC A–F DEMO)
    # =========================================================================
    else:
        st.sidebar.subheader("1. Intersection Selection")
        base_net, s_default, d_default = create_road_network()
        all_nodes = sorted(list(base_net.nodes()))
        all_edges = sorted(list(set(tuple(sorted([u, v])) for u, v in base_net.edges())))

        start_node = st.sidebar.selectbox("Start Intersection:", options=all_nodes, index=all_nodes.index(s_default))
        dest_node = st.sidebar.selectbox("Safe Destination:", options=all_nodes, index=all_nodes.index(d_default))

        st.sidebar.subheader("2. Road Closures (Edge Removal)")
        edge_labels = {e: f"Road {e[0]} <--> {e[1]} ({base_net[e[0]][e[1]]['distance']} units)" for e in all_edges}
        blocked_labels = st.sidebar.multiselect("Select Roads to Block:", options=list(edge_labels.values()), default=[])
        label_to_edge = {v: k for k, v in edge_labels.items()}
        blocked_edges = [label_to_edge[l] for l in blocked_labels]

        st.sidebar.subheader("3. K-Shortest Paths Parameter")
        top_k = st.sidebar.slider("Number of Simple Paths (K):", min_value=1, max_value=5, value=3)

        # Compute K-Shortest Simple Paths using NetworkX
        k_result = generate_simulated_k_shortest_paths(base_net, start_node, dest_node, blocked_edges, top_k=top_k)

        # Baseline UCS & Algorithm Comparison
        orig_ucs_route, orig_ucs_dist = uniform_cost_search(base_net, start_node, dest_node)
        orig_bfs_route, orig_bfs_dist = breadth_first_search(base_net, start_node, dest_node)
        orig_astar_route, orig_astar_dist = a_star_search(base_net, start_node, dest_node)

        # Header Metrics
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            if start_node == dest_node:
                st.metric("Status", "Origin = Destination", "0 units")
            elif k_result["success"]:
                st.metric("Status", "Paths Found", f"{len(k_result['routes'])} Distinct Routes")
            else:
                st.metric("Status", "UNREACHABLE", "Destination Isolated", delta_color="inverse")

        with col2:
            if k_result["success"]:
                st.metric("Optimal Distance", f"{k_result['routes'][0]['distance']} units")
            else:
                st.metric("Optimal Distance", "Infinite (Blocked)")

        with col3:
            st.metric("Active Blockages", f"{len(blocked_edges)} Closed Roads")

        with col4:
            st.metric("Algorithm Comparison", "BFS vs UCS vs A*", "Academic Demo")

        if not k_result["success"]:
            st.error(f"🚨 **CRITICAL ALERT: DESTINATION UNREACHABLE!**\n\n{k_result.get('error')}")

        tab_sim_map, tab_sim_paths, tab_algo_comp = st.tabs([
            "🗺️ Simulated Network Map",
            "📊 K-Shortest Simple Paths (NetworkX)",
            "🔬 Algorithm Comparison (BFS vs UCS vs A*)",
        ])

        with tab_sim_map:
            render_simulated_network_legend()
            selected_demo_route = None
            if k_result["success"]:
                route_labels = [
                    " -> ".join(route["path"])
                    for route in k_result["routes"]
                ]
                selected_route_label = st.selectbox(
                    "Select simulated route for navigation:",
                    options=route_labels,
                    key="simulated_navigation_route",
                )
                selected_demo_route = next(
                    route for route in k_result["routes"]
                    if " -> ".join(route["path"]) == selected_route_label
                )
            best_route = selected_demo_route["path"] if selected_demo_route else None
            if offline_mode:
                simulated_segments = [
                    (
                        SIMULATED_COORDINATES[first][0],
                        SIMULATED_COORDINATES[first][1],
                        SIMULATED_COORDINATES[second][0],
                        SIMULATED_COORDINATES[second][1],
                    )
                    for first, second in base_net.edges()
                ]
                blocked_segments = [
                    (
                        SIMULATED_COORDINATES[first][0],
                        SIMULATED_COORDINATES[first][1],
                        SIMULATED_COORDINATES[second][0],
                        SIMULATED_COORDINATES[second][1],
                    )
                    for first, second in blocked_edges
                ]
                displayed_routes = {}
                if selected_demo_route:
                    displayed_routes["primary"] = {
                        "color": "#2f855a",
                        "route_coords": [
                            list(SIMULATED_COORDINATES[node])
                            for node in selected_demo_route["path"]
                        ],
                    }
                st.markdown(
                    render_local_map_svg(
                        simulated_segments,
                        displayed_routes,
                        SIMULATED_COORDINATES[start_node],
                        SIMULATED_COORDINATES[dest_node],
                        f"Start {start_node}",
                        f"Destination {dest_node}",
                        blocked_segments=blocked_segments,
                    ),
                    unsafe_allow_html=True,
                )
                st.caption(
                    "Academic A–F simulation rendered locally as SVG. This is synthetic "
                    "demonstration data, not real-world road coverage."
                )
            else:
                folium_sim = build_simulated_network_map(
                    graph=base_net,
                    start_node=start_node,
                    dest_node=dest_node,
                    blocked_edges=blocked_edges,
                    current_route=best_route,
                )
            if selected_demo_route and not offline_mode:
                demo_path = selected_demo_route["path"]
                demo_instructions = [
                    {
                        "instruction": (
                            f"Follow the simulated route from intersection {node} "
                            f"to intersection {next_node} "
                            f"({base_net[node][next_node]['distance']} "
                            f"{'unit' if base_net[node][next_node]['distance'] == 1 else 'units'})."
                        ),
                        "location": list(SIMULATED_COORDINATES[next_node]),
                    }
                    for node, next_node in zip(demo_path, demo_path[1:])
                ]
                if len(demo_path) == 1:
                    demo_instructions.append({
                        "instruction": f"You are already at simulated intersection {demo_path[0]}.",
                        "location": list(SIMULATED_COORDINATES[demo_path[0]]),
                    })
                else:
                    demo_instructions.append({
                        "instruction": f"Arrive at simulated destination intersection {demo_path[-1]}.",
                        "location": list(SIMULATED_COORDINATES[demo_path[-1]]),
                    })
                add_navigation_overlay(
                    folium_sim,
                    instructions=demo_instructions,
                    route_coords=[list(SIMULATED_COORDINATES[node]) for node in demo_path],
                    navigation_key=(
                        "simulated:" + "-".join(demo_path) + ":"
                        + "-".join(f"{edge[0]}{edge[1]}" for edge in blocked_edges)
                    ),
                    allow_gps=False,
                )
            elif not selected_demo_route and not offline_mode:
                st_folium(folium_sim, width="100%", height=520)

        with tab_sim_paths:
            st.subheader(f"Top-{top_k} simulated A–F routes")
            st.caption(
                "NetworkX shortest_simple_paths results over the academic demo graph. "
                "Distances are graph units, not real road measurements; travel time is not modeled."
            )
            if k_result["success"]:
                route_cards = []
                selected_path = st.session_state.get("simulated_navigation_route")
                for idx, route_data in enumerate(k_result["routes"]):
                    path = route_data["path"]
                    path_label = " -> ".join(path)
                    detour = route_data["distance"] - k_result["routes"][0]["distance"]
                    route_cards.append({
                        "title": f"Route {idx + 1}: {path_label}",
                        "category": "NetworkX · simulated A–F graph",
                        "accent": "#0f766e" if idx == 0 else "#1d4e75",
                        "selected": (
                            path_label == selected_path
                            or (selected_path is None and idx == 0)
                        ),
                        "metrics": [
                            ("Graph distance", f"{route_data['distance']} units"),
                            ("Estimated time", "Not modeled"),
                        ],
                        "status": (
                            f"{len(blocked_edges)} simulated road closure(s) applied."
                            if blocked_edges
                            else "No simulated road closures selected."
                        ),
                        "details": [
                            (
                                "Shortest path in this result set."
                                if detour == 0
                                else (
                                    f"{detour} graph unit longer than the shortest result."
                                    if detour == 1
                                    else f"{detour} graph units longer than the shortest result."
                                )
                            ),
                            "Graph edges and coordinates are demonstration data, not real-world road routing.",
                        ],
                    })
                render_route_cards(route_cards)
            else:
                st.warning(
                    "No simulated route is available because the selected road closures disconnect "
                    "the destination. Clear one or more closures to recalculate."
                )

        with tab_algo_comp:
            st.subheader("BFS, UCS, and A* comparison")
            st.caption(
                "Unblocked baseline over the simulated graph. This academic comparison does not use live roads."
            )
            algorithm_results = [
                (
                    "Breadth-First Search (BFS)",
                    orig_bfs_route,
                    orig_bfs_dist,
                    "Fewest intersections; not necessarily the shortest distance.",
                    "#1d4e75",
                ),
                (
                    "Uniform Cost Search (UCS)",
                    orig_ucs_route,
                    orig_ucs_dist,
                    "Finds the shortest distance in the weighted demo graph.",
                    "#0f766e",
                ),
                (
                    "A* Search (h = 0)",
                    orig_astar_route,
                    orig_astar_dist,
                    "With a zero heuristic, behaves like Uniform Cost Search.",
                    "#315b7d",
                ),
            ]
            render_route_cards([
                {
                    "title": name,
                    "category": "Simulated A–F · unblocked baseline",
                    "accent": accent,
                    "metrics": [
                        ("Graph distance", f"{distance} units"),
                        ("Intersections", str(len(route)) if route else "None"),
                    ],
                    "status": "Travel time is not modeled.",
                    "details": [
                        "Route: " + (" -> ".join(route) if route else "No reachable route"),
                        optimality,
                    ],
                }
                for name, route, distance, optimality, accent in algorithm_results
            ])

    render_safety_disclaimer()


if __name__ == "__main__":
    main()
