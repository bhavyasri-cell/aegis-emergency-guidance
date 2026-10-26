"""
multi_route_planner.py
Multi-Route Evacuation Planning Engine for both Real-World and Simulated Road Networks.

Features:
1. Real-World Multi-Route Generation:
   - Primary Shortest-Distance Route (OSRM)
   - Distinct Alternative Route 1 (Lateral corridor via-point)
   - Distinct Alternative Route 2 (Secondary corridor via-point)
   - Hazard-Avoiding Route (Detects proximity to hazard and calculates bypass route)
   - Nearest Reachable Shelter Evaluation (Computes road distance to candidate shelters)
2. Simulated Graph K-Shortest Simple Paths (NetworkX shortest_simple_paths)
3. Duplicate route detection and distance-based recommendation rationale
"""

import math
import time
from typing import Dict, List, Optional, Tuple
import networkx as nx
from real_routing import extract_osrm_steps, geocode_location, get_real_road_route
import requests

HEADERS = {
    "User-Agent": "AIEmergencyEvacuationPlanner/2.0 (MultiRoute-AcademicDemo; contact: student-project)"
}
OSRM_ROUTE_URL = "https://router.project-osrm.org/route/v1/driving/{coords}"


def haversine_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Computes great-circle distance between two GPS points in kilometers."""
    r = 6371.0  # Earth's radius in km
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return r * c


def _point_to_segment_distance_km(
    point_lat: float,
    point_lon: float,
    start_lat: float,
    start_lon: float,
    end_lat: float,
    end_lon: float,
) -> float:
    """Returns the shortest spherical distance from a point to a route segment."""
    def to_vector(lat: float, lon: float) -> Tuple[float, float, float]:
        lat_rad, lon_rad = math.radians(lat), math.radians(lon)
        return (
            math.cos(lat_rad) * math.cos(lon_rad),
            math.cos(lat_rad) * math.sin(lon_rad),
            math.sin(lat_rad),
        )

    def dot(first: Tuple[float, float, float], second: Tuple[float, float, float]) -> float:
        return sum(a * b for a, b in zip(first, second))

    def cross(
        first: Tuple[float, float, float], second: Tuple[float, float, float]
    ) -> Tuple[float, float, float]:
        return (
            first[1] * second[2] - first[2] * second[1],
            first[2] * second[0] - first[0] * second[2],
            first[0] * second[1] - first[1] * second[0],
        )

    def angle(first: Tuple[float, float, float], second: Tuple[float, float, float]) -> float:
        cross_product = cross(first, second)
        cross_length = math.sqrt(dot(cross_product, cross_product))
        return math.atan2(cross_length, dot(first, second))

    point = to_vector(point_lat, point_lon)
    start = to_vector(start_lat, start_lon)
    end = to_vector(end_lat, end_lon)
    closest_angle = min(angle(point, start), angle(point, end))

    normal = cross(start, end)
    normal_length = math.sqrt(dot(normal, normal))
    if normal_length <= 1e-15:
        return 6371.0 * closest_angle

    normal = tuple(component / normal_length for component in normal)
    projection_scale = dot(point, normal)
    projection = tuple(
        point[index] - projection_scale * normal[index] for index in range(3)
    )
    projection_length = math.sqrt(dot(projection, projection))
    if projection_length <= 1e-15:
        return 6371.0 * closest_angle

    projection = tuple(component / projection_length for component in projection)
    segment_angle = angle(start, end)
    if angle(start, projection) + angle(projection, end) <= segment_angle + 1e-10:
        closest_angle = min(closest_angle, angle(point, projection))

    return 6371.0 * closest_angle


def check_route_hazard_proximity(
    route_coords: List[List[float]], hazard_lat: float, hazard_lon: float, radius_km: float
) -> Tuple[bool, float]:
    """
    Checks if any point along the road route passes within the hazard radius.

    Returns:
        tuple: (intersects_hazard: bool, min_distance_km: float)
    """
    if not route_coords or hazard_lat is None or hazard_lon is None:
        return False, float("inf")

    min_dist = float("inf")
    for pt in route_coords:
        dist = haversine_distance_km(pt[0], pt[1], hazard_lat, hazard_lon)
        if dist < min_dist:
            min_dist = dist

    for start, end in zip(route_coords, route_coords[1:]):
        dist = _point_to_segment_distance_km(
            hazard_lat,
            hazard_lon,
            start[0],
            start[1],
            end[0],
            end[1],
        )
        if dist < min_dist:
            min_dist = dist

    return min_dist <= radius_km, min_dist


def are_routes_distinct(coords1: List[List[float]], coords2: List[List[float]], threshold_km: float = 0.15) -> bool:
    """
    Compares routes at evenly spaced points so different vertex density or a
    shared midpoint does not make distinct provider geometries look identical.
    """
    if not coords1 or not coords2:
        return False

    def resample(coords: List[List[float]], sample_count: int = 51):
        points = []
        for point in coords:
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                return None
            try:
                latitude, longitude = float(point[0]), float(point[1])
            except (TypeError, ValueError):
                return None
            if not (
                math.isfinite(latitude)
                and math.isfinite(longitude)
                and -90 <= latitude <= 90
                and -180 <= longitude <= 180
            ):
                return None
            points.append((latitude, longitude))
        if not points:
            return None
        cumulative = [0.0]
        for start, end in zip(points, points[1:]):
            cumulative.append(
                cumulative[-1]
                + haversine_distance_km(start[0], start[1], end[0], end[1])
            )

        total_distance = cumulative[-1]
        if total_distance == 0:
            return [points[0]] * sample_count

        samples = []
        segment_index = 0
        for sample_index in range(sample_count):
            target = total_distance * sample_index / (sample_count - 1)
            while (
                segment_index < len(points) - 2
                and cumulative[segment_index + 1] < target
            ):
                segment_index += 1
            segment_distance = (
                cumulative[segment_index + 1] - cumulative[segment_index]
            )
            fraction = (
                0.0
                if segment_distance == 0
                else (target - cumulative[segment_index]) / segment_distance
            )
            start_lat, start_lon = points[segment_index]
            end_lat, end_lon = points[segment_index + 1]
            samples.append((
                start_lat + (end_lat - start_lat) * fraction,
                start_lon + (end_lon - start_lon) * fraction,
            ))
        return samples

    samples1 = resample(coords1)
    samples2 = resample(coords2)
    if samples1 is None or samples2 is None:
        return False
    return any(
        haversine_distance_km(lat1, lon1, lat2, lon2) >= threshold_km
        for (lat1, lon1), (lat2, lon2) in zip(samples1, samples2)
    )


def count_distinct_destination_routes(routes: Dict[str, Dict]) -> int:
    """Count distinct routes to the primary destination, excluding aliases/shelters."""
    return sum(
        1
        for route_id, route in routes.items()
        if route_id in {"primary", "alt_1", "alt_2", "alt_3", "hazard_avoid"}
        and not route.get("alias_of")
    )


def resolve_route_selection(
    routes: Dict[str, Dict], selected_route_id: str
) -> Tuple[Optional[str], Optional[Dict]]:
    """Return the requested route, falling back safely when that option is absent."""
    if selected_route_id in routes:
        return selected_route_id, routes[selected_route_id]
    if "primary" in routes:
        return "primary", routes["primary"]
    if routes:
        route_id = next(iter(routes))
        return route_id, routes[route_id]
    return None, None


def query_osrm_via(start_lat: float, start_lon: float, via_lat: float, via_lon: float, dest_lat: float, dest_lon: float) -> Optional[Dict]:
    """Queries OSRM driving route passing through an intermediate via-point."""
    coords_param = f"{start_lon:.6f},{start_lat:.6f};{via_lon:.6f},{via_lat:.6f};{dest_lon:.6f},{dest_lat:.6f}"
    url = OSRM_ROUTE_URL.format(coords=coords_param)
    params = {"overview": "full", "geometries": "geojson", "steps": "true"}

    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=12)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if data.get("code") != "Ok" or not data.get("routes"):
            return None

        primary = data["routes"][0]
        dist_m = primary.get("distance", 0.0)
        dur_s = primary.get("duration", 0.0)
        geojson_coords = primary.get("geometry", {}).get("coordinates", [])
        lat_lon_path = [[c[1], c[0]] for c in geojson_coords]

        steps = extract_osrm_steps(primary.get("legs", []))

        return {
            "success": True,
            "route_coords": lat_lon_path,
            "distance_km": round(dist_m / 1000.0, 2),
            "distance_m": round(dist_m, 1),
            "duration_mins": round(dur_s / 60.0, 1),
            "steps": steps,
        }
    except Exception:
        return None


def generate_real_world_multi_routes(
    start_lat: float,
    start_lon: float,
    dest_lat: float,
    dest_lon: float,
    hazard_info: Optional[Dict] = None,
    candidate_shelters: Optional[List[Dict]] = None,
) -> Dict[str, Dict]:
    """
    Uses OSRM-returned route alternatives for the primary comparison, then
    optionally evaluates a user-supplied simulated hazard and candidate shelter.

    Returns:
        dict: Keys are route identifiers, values are detailed route dictionaries.
    """
    routes = {}

    # OSRM supplies the primary route and any alternatives in one response.
    primary = get_real_road_route(start_lat, start_lon, dest_lat, dest_lon)
    if not primary.get("success"):
        return {"error": primary.get("error", "Failed to compute baseline road route.")}

    h_lat = hazard_info.get("lat") if hazard_info else None
    h_lon = hazard_info.get("lon") if hazard_info else None
    h_rad = hazard_info.get("radius_km", 0.5) if hazard_info else 0.5
    provider_routes = [primary, *primary.get("alternatives", [])]
    route_specs = [
        ("primary", "Shortest-Distance OSRM Route", "#2f855a"),
        ("alt_1", "OSRM Alternative Route 1", "#3182ce"),
        ("alt_2", "OSRM Alternative Route 2", "#805ad5"),
        ("alt_3", "OSRM Alternative Route 3", "#d53f8c"),
    ]
    accepted_provider_routes = []
    if not primary.get("route_coords") or not primary.get("success"):
        return {"error": "Routing service did not return a usable primary route."}
    usable_provider_routes = [
        route
        for route in provider_routes[:4]
        if route.get("success") and route.get("route_coords")
    ]
    usable_provider_routes.sort(
        key=lambda route: route.get(
            "distance_m",
            (route.get("distance_km") or math.inf) * 1000,
        )
    )

    for provider_route in usable_provider_routes:
        if not provider_route.get("route_coords"):
            continue
        if any(
            not are_routes_distinct(
                accepted["route_coords"], provider_route["route_coords"]
            )
            for accepted in accepted_provider_routes
        ):
            continue
        route_id, name, color = route_specs[len(accepted_provider_routes)]
        hit, distance = check_route_hazard_proximity(
            provider_route["route_coords"], h_lat, h_lon, h_rad
        )
        route = {
            "id": route_id,
            "name": name,
            "color": color,
            "distance_km": provider_route["distance_km"],
            "duration_mins": provider_route["duration_mins"],
            "route_coords": provider_route["route_coords"],
            "steps": provider_route.get("steps", []),
            "hazard_status": (
                f"Passes within {distance * 1000:.0f} m of simulated hazard zone."
                if hit
                else (
                    "No intersection with the user-specified simulated hazard buffer."
                    if hazard_info
                    else "Simulated hazard proximity not evaluated."
                )
            ),
            "is_compromised": hit,
            "rationale": (
                "Route geometry and estimates returned by OSRM. "
                "Included as a route alternative, not a safety ranking."
                if route_id == "primary"
                else "Distinct route alternative returned by OSRM. "
                "Included for comparison; no risk ranking is supported."
            ),
            "safety_disclaimer": (
                "OSRM route estimate; current conditions, closures, and safety "
                "are not verified."
            ),
        }
        routes[route_id] = route
        accepted_provider_routes.append(route)

    if not accepted_provider_routes:
        return {"error": "Routing service did not return a usable route geometry."}

    # 4. Hazard-Avoiding Route (If hazard specified and primary route intersects it)
    if hazard_info and h_lat is not None and h_lon is not None:
        # If any candidate route already exists and is completely clear of hazard, evaluate best clear path
        clear_candidates = [
            route for route in accepted_provider_routes
            if not route["is_compromised"]
        ]

        if clear_candidates:
            # Pick shortest clear candidate
            best_clear = min(clear_candidates, key=lambda x: x["distance_km"])
            best_clear_id = best_clear["id"]
            routes["hazard_avoid"] = {
                "id": "hazard_avoid",
                "name": "Hazard-Avoiding Route (existing corridor)",
                "color": best_clear["color"],
                "distance_km": best_clear["distance_km"],
                "duration_mins": best_clear["duration_mins"],
                "route_coords": best_clear["route_coords"],
                "steps": best_clear["steps"],
                "hazard_status": f"🛡️ Actively avoids {hazard_info.get('description', 'simulated hazard zone')} (buffer: {h_rad*1000:.0f}m)",
                "is_compromised": False,
                "alias_of": best_clear_id,
                "rationale": f"Explicitly verified to remain outside the {h_rad*1000:.0f}m radius of the simulated hazard zone.",
                "safety_disclaimer": "Simulated hazard avoidance only. Unverified against real-world disaster conditions.",
            }
        else:
            # Attempt a wider bypass via an evasion point away from the hazard
            mid_lat = (start_lat + dest_lat) / 2.0
            mid_lon = (start_lon + dest_lon) / 2.0
            evade_vec_x = mid_lon - h_lon
            evade_vec_y = mid_lat - h_lat
            norm = math.sqrt(evade_vec_x**2 + evade_vec_y**2) + 1e-6
            bypass_lat = h_lat + (evade_vec_y / norm) * (h_rad * 2.2 / 111.0)
            bypass_lon = h_lon + (evade_vec_x / norm) * (h_rad * 2.2 / (111.0 * math.cos(math.radians(mid_lat))))

            bypass = query_osrm_via(start_lat, start_lon, bypass_lat, bypass_lon, dest_lat, dest_lon)
            if bypass:
                b_hit, b_dist = check_route_hazard_proximity(bypass["route_coords"], h_lat, h_lon, h_rad)
                if not b_hit and all(
                    are_routes_distinct(
                        route["route_coords"], bypass["route_coords"]
                    )
                    for route in accepted_provider_routes
                ):
                    routes["hazard_avoid"] = {
                        "id": "hazard_avoid",
                        "name": "Hazard-Bypassing Route (Detour)",
                        "color": "#dd6b20",
                        "distance_km": bypass["distance_km"],
                        "duration_mins": bypass["duration_mins"],
                        "route_coords": bypass["route_coords"],
                        "steps": bypass["steps"],
                        "hazard_status": (
                            f"Calculated bypass around "
                            f"{hazard_info.get('description', 'simulated hazard')} "
                            f"(separation: {b_dist * 1000:.0f} m)"
                        ),
                        "is_compromised": False,
                        "rationale": (
                            "Detour specifically routed outside the "
                            "user-specified simulated danger buffer."
                        ),
                        "safety_disclaimer": (
                            "Simulated hazard avoidance only; current road "
                            "conditions and safety are not verified."
                        ),
                    }

    # 5. Candidate Shelters / Nearest Reachable Shelter Evaluation
    if candidate_shelters:
        shelter_results = []
        for sh in candidate_shelters:
            s_lat = sh.get("lat")
            s_lon = sh.get("lon")
            s_name = sh.get("name", "Candidate Shelter")
            if s_lat is not None and s_lon is not None:
                sh_route = get_real_road_route(start_lat, start_lon, s_lat, s_lon)
                if sh_route.get("success"):
                    shelter_results.append({
                        "name": s_name,
                        "lat": s_lat,
                        "lon": s_lon,
                        "distance_km": sh_route["distance_km"],
                        "duration_mins": sh_route["duration_mins"],
                        "route_coords": sh_route["route_coords"],
                        "steps": sh_route["steps"],
                    })

        if shelter_results:
            # Sort by road distance
            shelter_results.sort(key=lambda s: s["distance_km"])
            nearest = shelter_results[0]

            routes["nearest_shelter"] = {
                "id": "nearest_shelter",
                "name": f"Nearest Reachable Shelter: {nearest['name'][:30]}",
                "color": "#319795",  # Teal
                "distance_km": nearest["distance_km"],
                "duration_mins": nearest["duration_mins"],
                "route_coords": nearest["route_coords"],
                "steps": nearest["steps"],
                "hazard_status": "Candidate shelter route (User-provided, unverified status)",
                "is_compromised": False,
                "rationale": f"Minimum road distance among {len(shelter_results)} candidate shelters ({nearest['distance_km']} km).",
                "safety_disclaimer": "User-provided shelter location. Authorization by emergency officials is unverified.",
            }

    return routes


def generate_simulated_k_shortest_paths(graph, start: str, dest: str, blocked_roads: Optional[List[Tuple[str, str]]] = None, top_k: int = 3) -> Dict:
    """
    Computes k-shortest simple paths on the academic simulated graph using NetworkX.

    Returns:
        dict: List of distinct routes and distance metrics, or unreachable status.
    """
    working_graph = graph.copy()
    if blocked_roads:
        for u, v in blocked_roads:
            if working_graph.has_edge(u, v):
                working_graph.remove_edge(u, v)

    if start not in working_graph or dest not in working_graph:
        return {"success": False, "error": "Start or Destination node not found in graph."}

    try:
        # NetworkX shortest_simple_paths generates paths in strictly increasing order of total edge weight
        path_generator = nx.shortest_simple_paths(working_graph, start, dest, weight="distance")
        k_paths = []
        for _ in range(top_k):
            try:
                p = next(path_generator)
                # Compute distance
                dist = sum(working_graph[p[i]][p[i+1]]["distance"] for i in range(len(p) - 1))
                k_paths.append({
                    "path": p,
                    "distance": dist,
                    "hops": len(p) - 1,
                })
            except StopIteration:
                break

        if not k_paths:
            return {"success": False, "error": "Destination is completely unreachable (No path exists)."}

        return {"success": True, "routes": k_paths}

    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return {"success": False, "error": "Destination is completely unreachable (All paths blocked)."}


def test_multi_route_suite():
    """Unit tests for real-world multi-route generation and simulated K-shortest simple paths."""
    print("=" * 65)
    print("TESTING MULTI-ROUTE EVACUATION PLANNER SUITE")
    print("=" * 65)

    # 1. Test Real-World Multi-Route Generation
    start_addr = "Connaught Place, New Delhi"
    dest_addr = "India Gate, New Delhi"
    hazard_addr = "Janpath, New Delhi"

    print(f"\n1. Geocoding '{start_addr}'...")
    s_lat, s_lon, s_name = geocode_location(start_addr)
    time.sleep(1.0)
    print(f"2. Geocoding '{dest_addr}'...")
    d_lat, d_lon, d_name = geocode_location(dest_addr)
    time.sleep(1.0)
    print(f"3. Geocoding simulated hazard '{hazard_addr}'...")
    h_lat, h_lon, h_name = geocode_location(hazard_addr)

    hazard_info = {
        "lat": h_lat,
        "lon": h_lon,
        "radius_km": 0.35,
        "description": "Simulated Waterlogging / Obstruction on Janpath",
    }

    # Candidate Shelter
    candidate_shelter = {
        "name": "Major Dhyan Chand National Stadium",
        "lat": 28.6119,
        "lon": 77.2374,
    }

    print("\n4. Generating Multi-Route Suite (Primary, Alternatives, Hazard Avoidance, Nearest Shelter)...")
    routes = generate_real_world_multi_routes(
        start_lat=s_lat,
        start_lon=s_lon,
        dest_lat=d_lat,
        dest_lon=d_lon,
        hazard_info=hazard_info,
        candidate_shelters=[candidate_shelter],
    )

    print(f"\nDiscovered {len(routes)} distinct real road routes:")
    for key, r in routes.items():
        print(f"  • [{r['name']}]")
        print(f"    - Distance   : {r['distance_km']} km")
        print(f"    - Drive Time : {r['duration_mins']} mins")
        print(f"    - Hazard Info: {r['hazard_status']}")
        print(f"    - Color Code : {r['color']}")

    # 2. Test Simulated K-Shortest Simple Paths
    from road_network import create_road_network
    base_net, s_node, d_node = create_road_network()

    print("\n" + "-" * 65)
    print("5. Testing Simulated K-Shortest Simple Paths (NetworkX on A -> F):")
    sim_paths = generate_simulated_k_shortest_paths(base_net, "A", "F", top_k=3)
    if sim_paths["success"]:
        for idx, p in enumerate(sim_paths["routes"]):
            print(f"  Route {idx+1}: {' -> '.join(p['path'])} | Distance: {p['distance']} units | Hops: {p['hops']}")

    print("\n6. Testing Simulated Blocked Road Avoidance (Block B-D and C-E):")
    sim_blocked = generate_simulated_k_shortest_paths(base_net, "A", "F", blocked_roads=[("B", "D"), ("C", "E")], top_k=2)
    if sim_blocked["success"]:
        for idx, p in enumerate(sim_blocked["routes"]):
            print(f"  Rerouted {idx+1}: {' -> '.join(p['path'])} | Distance: {p['distance']} units")

    print("\n7. Testing Unreachable Destination (Block D-F and E-F):")
    sim_unreach = generate_simulated_k_shortest_paths(base_net, "A", "F", blocked_roads=[("D", "F"), ("E", "F")])
    print(f"  Unreachable Result: success={sim_unreach['success']}, error='{sim_unreach.get('error')}'")

    print("\n" + "=" * 65)
    print("ALL MULTI-ROUTE ENGINE TESTS COMPLETED SUCCESSFULLY!")
    print("=" * 65)


if __name__ == "__main__":
    test_multi_route_suite()
