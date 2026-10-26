"""
real_routing.py
Real-World Geocoding and Road Network Routing for Emergency Evacuation.

Uses:
1. OpenStreetMap Nominatim for geocoding real addresses/landmarks to GPS coordinates.
2. OpenStreetMap OSRM (Open Source Routing Machine) for real-world road graph navigation.
"""

import time
import os
import math
import threading
from typing import Dict, List, Optional, Tuple
import requests


# OpenStreetMap Nominatim Geocoding Endpoint
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"

# OpenStreetMap OSRM Public Driving Route API
OSRM_ROUTE_URL = "https://router.project-osrm.org/route/v1/driving/{coords}"

# Custom User-Agent compliant with OpenStreetMap Nominatim Usage Policy
HEADERS = {
    "User-Agent": (
        "AIEmergencyEvacuationPlanner/1.1"
        + (f" ({os.environ['NOMINATIM_CONTACT']})" if os.environ.get("NOMINATIM_CONTACT") else "")
    )
}
_NOMINATIM_REQUEST_LOCK = threading.Lock()
_LAST_NOMINATIM_REQUEST = 0.0
_GEOCODE_CACHE_LOCK = threading.Lock()
_GEOCODE_CACHE: Dict[str, Tuple[float, float, str, float]] = {}
GEOCODE_CACHE_TTL_SECONDS = 3600


def _positive_setting(name: str, default: float) -> float:
    """Read a positive numeric setting from the environment."""
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive number.") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{name} must be a positive number.")
    return parsed


def _nominatim_get(url: str, params: Dict, timeout: float):
    """Serialize Nominatim requests and enforce its one-request-per-second limit."""
    global _LAST_NOMINATIM_REQUEST
    with _NOMINATIM_REQUEST_LOCK:
        elapsed = time.monotonic() - _LAST_NOMINATIM_REQUEST
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)
        _LAST_NOMINATIM_REQUEST = time.monotonic()
        return requests.get(url, params=params, headers=HEADERS, timeout=timeout)


def geocode_location(
    query: str, timeout: Optional[float] = None
) -> Tuple[Optional[float], Optional[float], str]:
    """
    Geocodes an address, place name, or landmark into latitude and longitude coordinates.

    Args:
        query (str): The search query (e.g. 'Empire State Building, New York' or 'Connaught Place, New Delhi').
        timeout (float): Network timeout in seconds. Defaults to the
            NOMINATIM_TIMEOUT_SECONDS environment setting (8 seconds).

    Returns:
        tuple: (latitude, longitude, display_name_or_error_message)
               If successful, lat & lon are floats, and string is display name.
               If failed, lat & lon are None, and string is the error description.
    """
    if not isinstance(query, str) or not query.strip():
        return None, None, "Please enter a valid location or address."

    clean_query = query.strip()
    cache_key = clean_query.casefold()
    now = time.monotonic()
    with _GEOCODE_CACHE_LOCK:
        cached = _GEOCODE_CACHE.get(cache_key)
        if cached and cached[3] > now:
            return cached[0], cached[1], cached[2]
        if cached:
            del _GEOCODE_CACHE[cache_key]

    try:
        request_timeout = (
            _positive_setting("NOMINATIM_TIMEOUT_SECONDS", 8.0)
            if timeout is None
            else float(timeout)
        )
    except (TypeError, ValueError) as exc:
        return None, None, f"Invalid geocoding timeout configuration: {exc}"
    if not math.isfinite(request_timeout) or request_timeout <= 0:
        return None, None, "Geocoding timeout must be a positive number."

    params = {
        "q": clean_query,
        "format": "json",
        "limit": 1,
        "addressdetails": 1,
    }

    for attempt in range(2):
        try:
            response = _nominatim_get(NOMINATIM_URL, params, request_timeout)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            if attempt == 0:
                try:
                    retry_delay = _positive_setting("NOMINATIM_RETRY_DELAY_SECONDS", 0.25)
                except ValueError as config_error:
                    return None, None, f"Invalid geocoding retry configuration: {config_error}"
                time.sleep(retry_delay)
                continue
            if isinstance(exc, requests.exceptions.Timeout):
                return None, None, (
                    "Geocoding request timed out after one retry. "
                    "Press Generate Multi-Route Evacuation Plan to retry."
                )
            return None, None, (
                "Network error during geocoding after one retry. "
                "Check your connection and press Generate Multi-Route Evacuation Plan to retry."
            )
        except requests.exceptions.RequestException as exc:
            return None, None, f"Network error during geocoding: {exc}"

        if response.status_code == 429:
            return None, None, "Geocoding rate limit reached. Please wait a moment before searching again."

        if 500 <= response.status_code < 600 and attempt == 0:
            try:
                retry_delay = _positive_setting("NOMINATIM_RETRY_DELAY_SECONDS", 0.25)
            except ValueError as config_error:
                return None, None, f"Invalid geocoding retry configuration: {config_error}"
            time.sleep(retry_delay)
            continue
        if response.status_code != 200:
            return None, None, f"Geocoding service returned HTTP error {response.status_code}."

        try:
            results = response.json()
            if not isinstance(results, list):
                return None, None, "Geocoding service returned an invalid response."
            if not results:
                return None, None, f"Location '{clean_query}' could not be found. Please check spelling or specify city/country."

            first_match = results[0]
            lat = float(first_match["lat"])
            lon = float(first_match["lon"])
            display_name = first_match.get("display_name")
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                return None, None, "Geocoding service returned coordinates outside valid ranges."
            if not isinstance(display_name, str) or not display_name.strip():
                display_name = clean_query
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            return None, None, f"Error parsing geocoding response: {exc}"

        with _GEOCODE_CACHE_LOCK:
            _GEOCODE_CACHE[cache_key] = (
                lat,
                lon,
                display_name,
                time.monotonic() + GEOCODE_CACHE_TTL_SECONDS,
            )
        return lat, lon, display_name

    return None, None, "Geocoding service remained unavailable after one retry."


def reverse_geocode_location(
    latitude: float, longitude: float, timeout: int = 10
) -> Tuple[Optional[str], str]:
    """Returns an OSM display name for coordinates, or a non-fatal error message."""
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None, "Coordinates are outside valid latitude/longitude ranges."

    params = {
        "lat": f"{latitude:.7f}",
        "lon": f"{longitude:.7f}",
        "format": "jsonv2",
    }
    try:
        response = _nominatim_get(NOMINATIM_REVERSE_URL, params, timeout)
        if response.status_code == 429:
            return None, "Nominatim rate limit reached; showing coordinates instead."
        if response.status_code != 200:
            return None, f"Reverse-geocoding service returned HTTP {response.status_code}."

        result = response.json()
        display_name = result.get("display_name")
        if not isinstance(display_name, str) or not display_name.strip():
            return None, "No place name was found for these coordinates."
        return display_name, ""
    except requests.exceptions.Timeout:
        return None, "Reverse geocoding timed out; showing coordinates instead."
    except requests.exceptions.RequestException as e:
        return None, f"Reverse-geocoding network error: {e}"
    except (TypeError, ValueError) as e:
        return None, f"Could not parse reverse-geocoding response: {e}"


def extract_osrm_steps(legs: List[Dict]) -> List[Dict]:
    """Keep OSRM's maneuver metadata and coordinates without inventing road names."""
    steps = []
    for leg in legs:
        for step in leg.get("steps", []):
            maneuver = step.get("maneuver", {})
            instruction = maneuver.get("instruction")
            has_api_instruction = isinstance(instruction, str) and bool(instruction.strip())
            maneuver_type = maneuver.get("type", "")
            modifier = maneuver.get("modifier", "")
            road_name = step.get("name")
            road_name = road_name.strip() if isinstance(road_name, str) else ""

            if has_api_instruction:
                instruction = instruction.strip()
            elif maneuver_type == "arrive":
                instruction = "Arrive at the destination"
            elif maneuver_type == "depart":
                instruction = "Start along the route"
            elif maneuver_type in ("turn", "end of road") and modifier in (
                "left", "right", "straight", "slight left", "slight right",
                "sharp left", "sharp right", "uturn",
            ):
                turn = "Make a U-turn" if modifier == "uturn" else f"Turn {modifier}"
                instruction = turn
            elif maneuver_type in ("continue", "new name"):
                instruction = "Continue along the route"
            else:
                instruction = "Continue along the mapped route"

            if road_name and maneuver_type != "arrive" and not has_api_instruction:
                if maneuver_type == "depart":
                    instruction = f"{instruction} on {road_name}"
                elif maneuver_type in ("turn", "end of road"):
                    instruction = f"{instruction} onto {road_name}"
                elif maneuver_type in ("continue", "new name"):
                    instruction = f"{instruction} on {road_name}"

            location = maneuver.get("location")
            maneuver_location = (
                [location[1], location[0]]
                if isinstance(location, list) and len(location) >= 2
                else None
            )
            steps.append({
                "instruction": instruction,
                "distance_m": step.get("distance", 0.0),
                "duration_s": step.get("duration", 0.0),
                "name": road_name,
                "maneuver_type": maneuver_type,
                "maneuver_modifier": modifier,
                "maneuver_location": maneuver_location,
            })
    return steps


def get_real_road_route(
    start_lat: float,
    start_lon: float,
    dest_lat: float,
    dest_lon: float,
    timeout: int = 12,
) -> Dict:
    """
    Calculates the primary route and up to three provider-returned alternatives
    along the actual OpenStreetMap road network using OSRM.

    Args:
        start_lat, start_lon: Coordinates of starting origin.
        dest_lat, dest_lon: Coordinates of safe destination.

    Returns:
        dict: Containing route coordinates (lat/lon points), distance (km), duration (mins),
              turn-by-turn steps, and status information.
    """
    # OSRM expects coordinates in lon,lat format separated by semicolons
    coords_param = f"{start_lon:.6f},{start_lat:.6f};{dest_lon:.6f},{dest_lat:.6f}"
    url = OSRM_ROUTE_URL.format(coords=coords_param)
    params = {
        "overview": "full",
        "geometries": "geojson",
        "steps": "true",
        "alternatives": 3,
    }

    try:
        response = requests.get(url, params=params, headers=HEADERS, timeout=timeout)

        if response.status_code != 200:
            return {
                "success": False,
                "error": f"OSRM routing service returned HTTP {response.status_code}.",
            }

        try:
            data = response.json()
        except ValueError:
            return {
                "success": False,
                "error": "Routing service returned an invalid JSON response.",
            }
        if not isinstance(data, dict):
            return {
                "success": False,
                "error": "Routing service returned an unexpected response format.",
            }
        if data.get("code") != "Ok" or not data.get("routes"):
            return {
                "success": False,
                "error": data.get("message", "No drivable road route found between these coordinates."),
            }

        parsed_routes = [
            _normalize_osrm_route(route)
            for route in data["routes"][:4]
        ]
        parsed_routes = [route for route in parsed_routes if route is not None]
        if not parsed_routes:
            return {
                "success": False,
                "error": "Routing service did not return a usable route geometry.",
            }

        primary_route = parsed_routes[0]
        primary_route["alternatives"] = parsed_routes[1:]
        return primary_route

    except requests.exceptions.Timeout:
        return {
            "success": False,
            "error": "Routing service request timed out. Please check your internet connection.",
        }
    except requests.exceptions.RequestException as e:
        return {
            "success": False,
            "error": f"Network communication error with routing service: {str(e)}",
        }


def _normalize_osrm_route(route: object) -> Optional[Dict]:
    """Validate and normalize one provider-returned OSRM route."""
    if not isinstance(route, dict):
        return None
    try:
        distance_meters = float(route["distance"])
        duration_seconds = float(route["duration"])
        geojson = route["geometry"]
        coordinates = geojson["coordinates"]
    except (KeyError, TypeError, ValueError):
        return None
    if (
        not math.isfinite(distance_meters)
        or distance_meters < 0
        or not math.isfinite(duration_seconds)
        or duration_seconds < 0
        or not isinstance(coordinates, list)
    ):
        return None

    lat_lon_path = []
    for coordinate in coordinates:
        if not isinstance(coordinate, (list, tuple)) or len(coordinate) < 2:
            return None
        try:
            longitude, latitude = float(coordinate[0]), float(coordinate[1])
        except (TypeError, ValueError):
            return None
        if not (
            math.isfinite(latitude)
            and math.isfinite(longitude)
            and -90 <= latitude <= 90
            and -180 <= longitude <= 180
        ):
            return None
        lat_lon_path.append([latitude, longitude])
    if not lat_lon_path:
        return None

    legs = route.get("legs", [])
    return {
        "success": True,
        "route_coords": lat_lon_path,
        "distance_km": round(distance_meters / 1000.0, 2),
        "distance_m": round(distance_meters, 1),
        "duration_mins": round(duration_seconds / 60.0, 1),
        "steps": extract_osrm_steps(legs if isinstance(legs, list) else []),
    }


def test_real_location_workflow():
    """Tests geocoding and real road network routing with two real locations."""
    print("=" * 65)
    print("TESTING REAL-WORLD LOCATION EVACUATION ROUTE WORKFLOW")
    print("=" * 65)

    start_place = "Connaught Place, New Delhi"
    dest_place = "India Gate, New Delhi"

    print(f"\n1. Geocoding Starting Point: '{start_place}'...")
    s_lat, s_lon, s_name = geocode_location(start_place)
    if s_lat is None:
        print(f"  [Error] Failed to geocode start: {s_name}")
        return False
    print(f"  -> Found: {s_name}")
    print(f"  -> Coordinates: ({s_lat:.5f}, {s_lon:.5f})")

    # Brief courtesy pause to respect Nominatim policy
    time.sleep(1.0)

    print(f"\n2. Geocoding Safe Destination: '{dest_place}'...")
    d_lat, d_lon, d_name = geocode_location(dest_place)
    if d_lat is None:
        print(f"  [Error] Failed to geocode destination: {d_name}")
        return False
    print(f"  -> Found: {d_name}")
    print(f"  -> Coordinates: ({d_lat:.5f}, {d_lon:.5f})")

    print(f"\n3. Querying OpenStreetMap Road Network Routing (OSRM)...")
    route_result = get_real_road_route(s_lat, s_lon, d_lat, d_lon)

    if not route_result["success"]:
        print(f"  [Error] Routing failed: {route_result.get('error')}")
        return False

    print("  -> Road Route Successfully Computed!")
    print(f"  -> Total Road Distance  : {route_result['distance_km']} km ({route_result['distance_m']} meters)")
    print(f"  -> Estimated Drive Time : {route_result['duration_mins']} minutes")
    print(f"  -> Waypoint Coordinates : {len(route_result['route_coords'])} GPS points along real roads")
    print(f"  -> Turn-by-Turn Steps   : {len(route_result['steps'])} navigation instructions")

    print("\nSample Turn-by-Turn Steps:")
    for idx, step in enumerate(route_result["steps"][:5]):
        print(f"   Step {idx + 1}: {step['instruction']} ({step['distance_m']:.0f} m)")

    print("\n" + "=" * 65)
    print("REAL-WORLD LOCATION ROUTING VERIFIED SUCCESSFULLY!")
    print("=" * 65)
    return True


if __name__ == "__main__":
    test_real_location_workflow()
