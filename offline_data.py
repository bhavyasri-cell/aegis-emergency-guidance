"""Local GeoJSON and SQLite support for AEGIS offline route planning."""

from contextlib import contextmanager
import hashlib
import html
import json
import math
import os
import sqlite3
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import networkx as nx


DEFAULT_DATABASE_PATH = Path(__file__).resolve().parent / "data" / "aegis_offline.sqlite3"
MAX_SNAP_DISTANCE_KM = 1.0


def _database_path() -> Path:
    configured_path = os.environ.get("AEGIS_OFFLINE_DB")
    return Path(configured_path).expanduser() if configured_path else DEFAULT_DATABASE_PATH


def _connect() -> sqlite3.Connection:
    path = _database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS roads (
            road_id TEXT PRIMARY KEY,
            start_lat REAL NOT NULL,
            start_lon REAL NOT NULL,
            end_lat REAL NOT NULL,
            end_lon REAL NOT NULL,
            distance_km REAL NOT NULL,
            name TEXT NOT NULL,
            source TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS places (
            place_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            category TEXT NOT NULL,
            source TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reports (
            provider TEXT NOT NULL,
            coordinate_key TEXT NOT NULL,
            retrieved_at_utc TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (provider, coordinate_key)
        );
        CREATE INDEX IF NOT EXISTS idx_places_name ON places(name COLLATE NOCASE);
        """
    )
    return connection


@contextmanager
def _connection():
    connection = _connect()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _coordinates_valid(latitude: object, longitude: object) -> bool:
    if isinstance(latitude, bool) or isinstance(longitude, bool):
        return False
    try:
        lat, lon = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return False
    return math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180


def _distance_km(first: Sequence[float], second: Sequence[float]) -> float:
    latitude_1, longitude_1 = map(math.radians, first)
    latitude_2, longitude_2 = map(math.radians, second)
    delta_latitude = latitude_2 - latitude_1
    delta_longitude = longitude_2 - longitude_1
    value = (
        math.sin(delta_latitude / 2) ** 2
        + math.cos(latitude_1)
        * math.cos(latitude_2)
        * math.sin(delta_longitude / 2) ** 2
    )
    return 6371.0 * 2 * math.atan2(math.sqrt(value), math.sqrt(max(0.0, 1 - value)))


def database_summary() -> Dict:
    """Return local road and saved-place counts without accessing the network."""
    with _connection() as connection:
        roads = connection.execute("SELECT COUNT(*) FROM roads").fetchone()[0]
        places = connection.execute("SELECT COUNT(*) FROM places").fetchone()[0]
        shelters = connection.execute(
            "SELECT COUNT(*) FROM places WHERE category = 'shelter'"
        ).fetchone()[0]
        bounds = connection.execute(
            """
            SELECT MIN(latitude), MAX(latitude), MIN(longitude), MAX(longitude)
            FROM (
                SELECT start_lat AS latitude, start_lon AS longitude FROM roads
                UNION ALL
                SELECT end_lat, end_lon FROM roads
            )
            """
        ).fetchone()
        sources = connection.execute(
            "SELECT DISTINCT source FROM roads ORDER BY source LIMIT 10"
        ).fetchall()
    coverage = None
    if roads and bounds[0] is not None:
        coverage = {
            "min_latitude": bounds[0],
            "max_latitude": bounds[1],
            "min_longitude": bounds[2],
            "max_longitude": bounds[3],
        }
    return {
        "road_segments": roads,
        "places": places,
        "shelters": shelters,
        "coverage_bounds": coverage,
        "sources": [row["source"] for row in sources],
    }


def import_geojson(geojson_text: str, source_name: str = "Imported GeoJSON") -> Dict:
    """Import roads and named Point features from a GeoJSON FeatureCollection.

    Road geometries must be LineString/MultiLineString. Point features may set
    ``category`` to ``shelter`` or ``hazard``; other points become saved places.
    Every imported feature must have usable coordinates, and roads need a name
    or stable source identifier.
    """
    try:
        document = json.loads(geojson_text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("The selected file is not valid JSON/GeoJSON.") from exc
    if not isinstance(document, dict) or document.get("type") != "FeatureCollection":
        raise ValueError("Expected a GeoJSON FeatureCollection.")
    features = document.get("features")
    if not isinstance(features, list):
        raise ValueError("GeoJSON FeatureCollection must contain a features list.")

    source = str(source_name or "Imported GeoJSON")[:250]
    road_count = place_count = shelter_count = skipped_count = 0
    with _connection() as connection:
        try:
            for feature_index, feature in enumerate(features):
                if not isinstance(feature, dict) or feature.get("type") != "Feature":
                    skipped_count += 1
                    continue
                properties = feature.get("properties")
                geometry = feature.get("geometry")
                if not isinstance(properties, dict) or not isinstance(geometry, dict):
                    skipped_count += 1
                    continue
                geometry_type = geometry.get("type")
                coordinates = geometry.get("coordinates")
                feature_name = (
                    properties.get("name")
                    or properties.get("ref")
                    or properties.get("id")
                )
                if not isinstance(feature_name, str) or not feature_name.strip():
                    feature_name = None

                if geometry_type in {"LineString", "MultiLineString"}:
                    lines = [coordinates] if geometry_type == "LineString" else coordinates
                    if not isinstance(lines, list):
                        raise ValueError(
                            f"Road feature {feature_index + 1} has malformed {geometry_type} coordinates."
                        )
                    segment_index = 0
                    for line in lines:
                        if not isinstance(line, list) or len(line) < 2:
                            raise ValueError(
                                f"Road feature {feature_index + 1} must contain at least two coordinates per line."
                            )
                        points: List[Tuple[float, float]] = []
                        for point in line:
                            if not isinstance(point, (list, tuple)) or len(point) < 2:
                                raise ValueError(
                                    f"Road feature {feature_index + 1} contains malformed coordinates."
                                )
                            longitude, latitude = point[0], point[1]
                            if not _coordinates_valid(latitude, longitude):
                                raise ValueError(
                                    f"Road feature {feature_index + 1} contains out-of-range coordinates."
                                )
                            points.append((float(latitude), float(longitude)))
                        for first, second in zip(points, points[1:]):
                            distance = _distance_km(first, second)
                            if distance <= 0:
                                continue
                            identity = (
                                properties.get("id")
                                or properties.get("osm_id")
                                or f"{source}:{feature_index}"
                            )
                            segment_identity = (
                                f"{identity}:{segment_index}:"
                                f"{first[0]:.7f},{first[1]:.7f}:"
                                f"{second[0]:.7f},{second[1]:.7f}"
                            )
                            road_id = hashlib.sha256(
                                segment_identity.encode("utf-8")
                            ).hexdigest()
                            connection.execute(
                                """
                                INSERT OR REPLACE INTO roads
                                (road_id, start_lat, start_lon, end_lat, end_lon,
                                 distance_km, name, source)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    road_id,
                                    first[0],
                                    first[1],
                                    second[0],
                                    second[1],
                                    distance,
                                    feature_name or "Unnamed local road",
                                    source,
                                ),
                            )
                            segment_index += 1
                            road_count += 1
                    continue

                if geometry_type != "Point" or not isinstance(coordinates, (list, tuple)):
                    raise ValueError(
                        f"Feature {feature_index + 1} uses unsupported geometry "
                        f"{geometry_type!r}; roads must be LineString/MultiLineString "
                        "and saved places, shelters, or hazard points must be Point features."
                    )
                if len(coordinates) < 2 or not _coordinates_valid(
                    coordinates[1], coordinates[0]
                ):
                    raise ValueError(
                        f"Point feature {feature_index + 1} contains invalid coordinates."
                    )
                if feature_name is None:
                    skipped_count += 1
                    continue
                category = str(
                    properties.get("category")
                    or properties.get("aegis_type")
                    or "place"
                ).strip().casefold()
                if category not in {"place", "shelter", "hazard"}:
                    category = "place"
                identity = properties.get("id") or properties.get("osm_id")
                place_id = str(identity or f"{source}:{feature_index}:{feature_name}")
                connection.execute(
                    """
                    INSERT OR REPLACE INTO places
                    (place_id, name, latitude, longitude, category, source)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        place_id,
                        feature_name.strip(),
                        float(coordinates[1]),
                        float(coordinates[0]),
                        category,
                        source,
                    ),
                )
                place_count += 1
                if category == "shelter":
                    shelter_count += 1
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return {
        "road_segments": road_count,
        "places": place_count,
        "shelters": shelter_count,
        "skipped_features": skipped_count,
        "source": source,
    }


def search_local_places(query: str, category: Optional[str] = None) -> Dict:
    """Search named places imported from local GeoJSON, with no network fallback."""
    if not isinstance(query, str) or not query.strip():
        return {"success": False, "error": "Enter a name to search local places.", "results": []}
    normalized = query.strip().casefold()
    with _connection() as connection:
        if category:
            rows = connection.execute(
                """
                SELECT place_id, name, latitude, longitude, category, source
                FROM places
                WHERE category = ? AND instr(lower(name), ?) > 0
                ORDER BY CASE WHEN lower(name) = ? THEN 0 ELSE 1 END, name
                LIMIT 20
                """,
                (category, normalized, normalized),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT place_id, name, latitude, longitude, category, source
                FROM places
                WHERE instr(lower(name), ?) > 0
                ORDER BY CASE WHEN lower(name) = ? THEN 0 ELSE 1 END, name
                LIMIT 20
                """,
                (normalized, normalized),
            ).fetchall()
    results = [dict(row) for row in rows]
    if not results:
        return {
            "success": False,
            "error": f"No local place matched '{query.strip()}'. No online lookup was attempted.",
            "results": [],
        }
    if len(results) > 1 and results[0]["name"].casefold() != normalized:
        return {
            "success": False,
            "error": (
                "The local search is ambiguous. Refine the name; matches include: "
                + ", ".join(result["name"] for result in results[:5])
            ),
            "results": results,
        }
    return {"success": True, "place": results[0], "results": results}


def _road_graph(connection: sqlite3.Connection) -> nx.Graph:
    graph = nx.Graph()
    rows = connection.execute(
        """
        SELECT start_lat, start_lon, end_lat, end_lon, distance_km, name
        FROM roads
        """
    )
    for row in rows:
        first = (row["start_lat"], row["start_lon"])
        second = (row["end_lat"], row["end_lon"])
        graph.add_node(first, latitude=first[0], longitude=first[1])
        graph.add_node(second, latitude=second[0], longitude=second[1])
        graph.add_edge(
            first,
            second,
            distance_km=row["distance_km"],
            name=row["name"],
        )
    return graph


def _nearest_node(
    graph: nx.Graph, latitude: float, longitude: float
) -> Tuple[Optional[Tuple[float, float]], float]:
    if graph.number_of_nodes() == 0:
        return None, math.inf
    nearest = min(
        graph.nodes,
        key=lambda node: _distance_km((latitude, longitude), node),
    )
    return nearest, _distance_km((latitude, longitude), nearest)


def generate_local_routes(
    start_latitude: float,
    start_longitude: float,
    destination_latitude: float,
    destination_longitude: float,
    limit: int = 3,
) -> Dict:
    """Calculate shortest local road-graph routes, without online routing services."""
    if not all(
        _coordinates_valid(lat, lon)
        for lat, lon in (
            (start_latitude, start_longitude),
            (destination_latitude, destination_longitude),
        )
    ):
        return {"error": "Route planning requires valid start and destination coordinates."}
    with _connection() as connection:
        graph = _road_graph(connection)
    if graph.number_of_edges() == 0:
        return {
            "error": (
                "No offline road dataset is installed. Import a GeoJSON road "
                "FeatureCollection that covers both selected locations."
            )
        }
    start_node, start_snap_km = _nearest_node(graph, start_latitude, start_longitude)
    destination_node, destination_snap_km = _nearest_node(
        graph, destination_latitude, destination_longitude
    )
    if start_snap_km > MAX_SNAP_DISTANCE_KM or destination_snap_km > MAX_SNAP_DISTANCE_KM:
        return {
            "error": (
                "One or both selected locations are outside the installed road "
                f"coverage (maximum road snap distance {MAX_SNAP_DISTANCE_KM:.1f} km)."
            )
        }
    if not nx.has_path(graph, start_node, destination_node):
        return {"error": "The installed local road network has no connected route between these locations."}

    try:
        candidates = nx.shortest_simple_paths(graph, start_node, destination_node, weight="distance_km")
        routes: Dict[str, Dict] = {}
        colors = ["#2f855a", "#3182ce", "#805ad5"]
        for index, nodes in enumerate(candidates):
            if index >= max(1, min(int(limit), 5)):
                break
            route_id = "primary" if index == 0 else f"alt_{index}"
            distance = sum(
                graph[first][second]["distance_km"]
                for first, second in zip(nodes, nodes[1:])
            )
            coordinates = [[node[0], node[1]] for node in nodes]
            routes[route_id] = {
                "id": route_id,
                "name": "Local Road Route" if index == 0 else f"Local Alternative {index}",
                "color": colors[index % len(colors)],
                "distance_km": round(distance, 2),
                "duration_mins": None,
                "route_coords": coordinates,
                "steps": [
                    {
                        "instruction": f"Continue on {graph[first][second].get('name') or 'local road'}",
                        "distance_m": _distance_km(first, second) * 1000,
                    }
                    for first, second in zip(nodes, nodes[1:])
                ],
                "hazard_status": "Local hazards were not evaluated.",
                "is_compromised": False,
                "rationale": (
                    "Shortest distance in the installed local road graph. "
                    "No traffic or current road-closure data is implied."
                    if index == 0
                    else "Alternative path in the installed local road graph; "
                    "no safety ranking is implied."
                ),
                "safety_disclaimer": (
                    "Offline local road geometry; road access, closures, and current "
                    "conditions are unverified."
                ),
                "source": "Imported local GeoJSON road data",
                "snap_distance_start_km": round(start_snap_km, 3),
                "snap_distance_destination_km": round(destination_snap_km, 3),
                "route_geometry_note": (
                    "Geometry contains only imported road-network vertices. "
                    "Origin/destination snap gaps are not represented as roads."
                ),
            }
        return routes or {"error": "No usable local route was found."}
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return {"error": "The installed local road network has no connected route between these locations."}


def save_cached_report(provider: str, latitude: float, longitude: float, report: Dict) -> None:
    """Persist a successful online report for transparent offline viewing later."""
    if not isinstance(report, dict) or not report.get("success"):
        return
    retrieved_at = report.get("retrieved_at_utc") or report.get("checked_at_utc")
    if not isinstance(retrieved_at, str):
        return
    coordinate_key = f"{float(latitude):.6f},{float(longitude):.6f}"
    with _connection() as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO reports
            (provider, coordinate_key, retrieved_at_utc, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (provider, coordinate_key, retrieved_at, json.dumps(report)),
        )


def load_cached_report(provider: str, latitude: float, longitude: float) -> Optional[Dict]:
    """Load a previously saved source response; it is never presented as live data."""
    coordinate_key = f"{float(latitude):.6f},{float(longitude):.6f}"
    with _connection() as connection:
        row = connection.execute(
            """
            SELECT payload_json FROM reports
            WHERE provider = ? AND coordinate_key = ?
            """,
            (provider, coordinate_key),
        ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload_json"])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def render_local_map_svg(
    road_segments: Iterable[Sequence[float]],
    routes: Dict,
    start: Sequence[float],
    destination: Sequence[float],
    start_name: str,
    destination_name: str,
    blocked_segments: Optional[Iterable[Sequence[float]]] = None,
    markers: Optional[Iterable[Dict]] = None,
) -> str:
    """Render a self-contained SVG road map that needs no tiles or JavaScript."""
    focus_points: List[Tuple[float, float]] = [
        (float(start[0]), float(start[1])),
        (float(destination[0]), float(destination[1])),
    ]
    for route in routes.values():
        focus_points.extend(
            (float(point[0]), float(point[1]))
            for point in route.get("route_coords", [])
        )
    marker_list = list(markers or [])
    for marker in marker_list:
        if _coordinates_valid(marker.get("latitude"), marker.get("longitude")):
            focus_points.append(
                (float(marker["latitude"]), float(marker["longitude"]))
            )
    focus_min_lat = min(point[0] for point in focus_points)
    focus_max_lat = max(point[0] for point in focus_points)
    focus_min_lon = min(point[1] for point in focus_points)
    focus_max_lon = max(point[1] for point in focus_points)
    focus_lat_span = max(focus_max_lat - focus_min_lat, 0.002)
    focus_lon_span = max(focus_max_lon - focus_min_lon, 0.002)
    margin_lat = focus_lat_span * 0.15
    margin_lon = focus_lon_span * 0.15
    road_list = [
        segment
        for segment in road_segments
        if (
            focus_min_lat - margin_lat <= float(segment[0]) <= focus_max_lat + margin_lat
            and focus_min_lat - margin_lat <= float(segment[2]) <= focus_max_lat + margin_lat
            and focus_min_lon - margin_lon <= float(segment[1]) <= focus_max_lon + margin_lon
            and focus_min_lon - margin_lon <= float(segment[3]) <= focus_max_lon + margin_lon
        )
    ]
    blocked_list = [
        segment
        for segment in (blocked_segments or [])
        if (
            focus_min_lat - margin_lat <= float(segment[0]) <= focus_max_lat + margin_lat
            and focus_min_lat - margin_lat <= float(segment[2]) <= focus_max_lat + margin_lat
            and focus_min_lon - margin_lon <= float(segment[1]) <= focus_max_lon + margin_lon
            and focus_min_lon - margin_lon <= float(segment[3]) <= focus_max_lon + margin_lon
        )
    ]
    blocked_keys = {
        tuple(sorted(
            ((float(segment[0]), float(segment[1])),
             (float(segment[2]), float(segment[3])))
        ))
        for segment in blocked_list
    }
    road_list = [
        segment for segment in road_list
        if tuple(sorted(
            ((float(segment[0]), float(segment[1])),
             (float(segment[2]), float(segment[3])))
        )) not in blocked_keys
    ]
    all_points = list(focus_points)
    for segment in road_list + blocked_list:
        all_points.extend(((float(segment[0]), float(segment[1])), (float(segment[2]), float(segment[3]))))
    min_lat = min(point[0] for point in all_points)
    max_lat = max(point[0] for point in all_points)
    min_lon = min(point[1] for point in all_points)
    max_lon = max(point[1] for point in all_points)
    lat_span = max(max_lat - min_lat, 0.002)
    lon_span = max(max_lon - min_lon, 0.002)
    padding = 34
    width, height = 1000, 560
    plot_width, plot_height = width - padding * 2, height - padding * 2
    longitude_scale = plot_width / lon_span
    latitude_scale = plot_height / lat_span
    scale = min(longitude_scale, latitude_scale)
    x_offset = (width - lon_span * scale) / 2
    y_offset = (height - lat_span * scale) / 2

    def project(latitude: float, longitude: float) -> Tuple[float, float]:
        return (
            x_offset + (longitude - min_lon) * scale,
            height - (y_offset + (latitude - min_lat) * scale),
        )

    items = [
        '<div role="img" aria-label="Offline local road map from imported geographic data">',
        '<svg viewBox="0 0 1000 560" style="width:100%;height:auto;background:#f8fafc;border:1px solid #cbd5e1;border-radius:12px" xmlns="http://www.w3.org/2000/svg">',
    ]
    for segment in road_list:
        first = project(float(segment[0]), float(segment[1]))
        second = project(float(segment[2]), float(segment[3]))
        items.append(
            f'<line x1="{first[0]:.1f}" y1="{first[1]:.1f}" '
            f'x2="{second[0]:.1f}" y2="{second[1]:.1f}" '
            'stroke="#94a3b8" stroke-width="2" />'
        )
    for segment in blocked_list:
        first = project(float(segment[0]), float(segment[1]))
        second = project(float(segment[2]), float(segment[3]))
        items.append(
            f'<line x1="{first[0]:.1f}" y1="{first[1]:.1f}" '
            f'x2="{second[0]:.1f}" y2="{second[1]:.1f}" '
            'stroke="#c53030" stroke-width="5" stroke-dasharray="9 7" />'
        )
    for route in routes.values():
        points = [
            project(float(point[0]), float(point[1]))
            for point in route.get("route_coords", [])
        ]
        if len(points) < 2:
            continue
        polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
        color = html.escape(str(route.get("color", "#0f766e")), quote=True)
        items.append(
            f'<polyline points="{polyline}" fill="none" stroke="{color}" '
            'stroke-width="5" stroke-linecap="round" stroke-linejoin="round" opacity="0.9" />'
        )
    for coordinates, label, color in (
        (start, start_name, "#16803c"),
        (destination, destination_name, "#c53030"),
    ):
        x, y = project(float(coordinates[0]), float(coordinates[1]))
        items.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="8" fill="{color}" stroke="white" stroke-width="3"/>'
            f'<text x="{x + 12:.1f}" y="{y - 10:.1f}" '
            'font-family="Arial,sans-serif" font-size="13" font-weight="700" '
            f'fill="#172554">{html.escape(str(label)[:60])}</text>'
        )
    for marker in marker_list:
        if not _coordinates_valid(marker.get("latitude"), marker.get("longitude")):
            continue
        x, y = project(float(marker["latitude"]), float(marker["longitude"]))
        color = "#E53935" if marker.get("category") == "hazard" else "#1565C0"
        label = html.escape(str(marker.get("name") or marker.get("category") or "Local marker")[:60])
        items.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="7" fill="{color}" '
            'stroke="white" stroke-width="2"/>'
            f'<text x="{x + 10:.1f}" y="{y + 17:.1f}" '
            'font-family="Arial,sans-serif" font-size="12" font-weight="700" '
            f'fill="#000000">{label}</text>'
        )
    items.append("</svg>")
    items.append(
        '<p style="margin:.5rem 0;color:#475569;font:13px Arial,sans-serif">'
        "Offline local road geometry · Imported data coverage only · "
        "Not a live basemap, traffic layer, or safety assessment</p></div>"
    )
    return "".join(items)


def local_road_segments() -> List[Tuple[float, float, float, float]]:
    with _connection() as connection:
        rows = connection.execute(
            "SELECT start_lat, start_lon, end_lat, end_lon FROM roads"
        ).fetchall()
    return [
        (row["start_lat"], row["start_lon"], row["end_lat"], row["end_lon"])
        for row in rows
    ]
