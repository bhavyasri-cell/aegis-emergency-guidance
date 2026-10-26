"""Official location-based disaster alerts from the NDMA SACHET portal."""

from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import threading
import time
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests


SACHET_LOCATION_ALERTS_URL = (
    "https://sachet.ndma.gov.in/cap_public_website/FetchLocationWiseAlerts"
)
SACHET_ALERT_PORTAL_URL = "https://sachet.ndma.gov.in/CapFeed/Feed"
SACHET_ALERT_RADIUS_KM = 20
ALERT_TIMEOUT_SECONDS = 12
ALERT_CACHE_TTL_SECONDS = 300
ALERT_STALE_AFTER_SECONDS = 30 * 60

_CACHE_LOCK = threading.Lock()
_SUCCESS_CACHE: Dict[str, Tuple[float, Dict]] = {}
_INDIA_TIMEZONE = ZoneInfo("Asia/Kolkata")


def _valid_coordinates(latitude: object, longitude: object) -> bool:
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError):
        return False
    return (
        math.isfinite(lat)
        and math.isfinite(lon)
        and -90 <= lat <= 90
        and -180 <= lon <= 180
    )


def _parse_datetime(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    # SACHET portal records use strings such as "Wed Oct 21 15:00:00 IST 2026".
    try:
        return datetime.strptime(
            text, "%a %b %d %H:%M:%S IST %Y"
        ).replace(tzinfo=_INDIA_TIMEZONE).astimezone(timezone.utc)
    except ValueError:
        return None


def alert_time_state(
    alert: Dict, now: Optional[datetime] = None
) -> str:
    """Classify supplied effective times without guessing missing timestamps."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)

    start_value = alert.get("effective_from")
    expiry_value = alert.get("expires_at")
    start_supplied = start_value is not None and (
        not isinstance(start_value, str) or bool(start_value.strip())
    )
    expiry_supplied = expiry_value is not None and (
        not isinstance(expiry_value, str) or bool(expiry_value.strip())
    )
    starts = _parse_datetime(start_value)
    expires = _parse_datetime(expiry_value)
    if expires is not None and expires <= current:
        return "expired"
    if expiry_supplied and expires is None:
        return "time_not_supplied"
    if starts is not None and starts > current:
        return "scheduled"
    if start_supplied and starts is None:
        return "time_not_supplied"
    if starts is None and expires is None:
        return "time_not_supplied"
    return "current"


def _validated_geometry(value: object) -> Optional[Dict]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(value, dict):
        return None
    if value.get("type") == "Feature":
        value = value.get("geometry")
    if not isinstance(value, dict):
        return None
    geometry_type = value.get("type")
    coordinates = value.get("coordinates")
    if geometry_type not in {"Polygon", "MultiPolygon"} or not isinstance(coordinates, list):
        return None

    polygons = [coordinates] if geometry_type == "Polygon" else coordinates
    if not polygons:
        return None
    for polygon in polygons:
        if not isinstance(polygon, list) or not polygon:
            return None
        for ring in polygon:
            if not isinstance(ring, list) or len(ring) < 4:
                return None
            for point in ring:
                if not isinstance(point, (list, tuple)) or len(point) < 2:
                    return None
                try:
                    lon, lat = float(point[0]), float(point[1])
                except (TypeError, ValueError):
                    return None
                if not (
                    math.isfinite(lon)
                    and math.isfinite(lat)
                    and -180 <= lon <= 180
                    and -90 <= lat <= 90
                ):
                    return None
    return value


def _official_link(raw: Dict) -> str:
    value = raw.get("url") or raw.get("web")
    if isinstance(value, str) and urlparse(value).scheme == "https":
        return value
    return SACHET_ALERT_PORTAL_URL


def _normalize_alert(raw: object) -> Optional[Dict]:
    if not isinstance(raw, dict):
        return None
    identifier = raw.get("identifier") or raw.get("alert_id_sdma_autoinc")
    if identifier is None:
        identity_fields = (
            "disaster_type",
            "effective_start_time",
            "effective_end_time",
            "area_description",
            "alert_source",
        )
        identity_values = [str(raw.get(key, "")).strip() for key in identity_fields]
        if not any(identity_values):
            return None
        identity = "|".join(identity_values)
        identifier = identity
    if not str(identifier).strip():
        return None

    return {
        "id": str(identifier),
        "title": (
            raw.get("headline")
            or raw.get("title")
            or raw.get("event")
            or raw.get("disaster_type")
            or "Official alert"
        ),
        "hazard_type": raw.get("disaster_type") or "Not supplied by source",
        "severity": raw.get("severity") or "Not supplied by source",
        "severity_level": raw.get("severity_level"),
        "affected_region": raw.get("area_description") or "Not supplied by source",
        "issuing_authority": raw.get("alert_source") or "Not supplied by source",
        "issue_time": (
            raw.get("sent")
            or raw.get("issue_time")
            or raw.get("issued_at")
        ),
        "effective_from": raw.get("effective_start_time"),
        "expires_at": raw.get("effective_end_time") or raw.get("expires"),
        "message": raw.get("warning_message"),
        "geometry": _validated_geometry(
            raw.get("area_json") or raw.get("area") or raw.get("geometry")
        ),
        "official_url": _official_link(raw),
        "alert_url_is_specific": _official_link(raw) != SACHET_ALERT_PORTAL_URL,
        "portal_url": SACHET_ALERT_PORTAL_URL,
    }


def clear_alert_cache() -> None:
    """Clear successful in-process alert responses (primarily useful in tests)."""
    with _CACHE_LOCK:
        _SUCCESS_CACHE.clear()


def fetch_location_alerts(
    latitude: float,
    longitude: float,
    timeout: int = ALERT_TIMEOUT_SECONDS,
) -> Dict:
    """Fetch alerts that the SACHET portal returns for a location's 20 km radius.

    The portal applies its own location/radius search. AEGIS determines whether an
    alert applies to a point or route only from the returned alert geometry.
    Successful responses, including an empty alert list, are cached briefly.
    Failures are never cached.
    """
    checked_at = datetime.now(timezone.utc).isoformat()
    if not _valid_coordinates(latitude, longitude):
        return {
            "success": False,
            "error": "Official alerts require valid latitude and longitude coordinates.",
            "checked_at_utc": checked_at,
        }

    lat = float(latitude)
    lon = float(longitude)
    cache_key = f"{lat:.5f},{lon:.5f}"
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _SUCCESS_CACHE.get(cache_key)
        if cached and cached[0] > now:
            return deepcopy(cached[1])
        if cached:
            del _SUCCESS_CACHE[cache_key]

    try:
        response = requests.post(
            SACHET_LOCATION_ALERTS_URL,
            params={
                "lat": lat,
                "long": lon,
                "radius": str(SACHET_ALERT_RADIUS_KM),
            },
            headers={
                "Accept": "application/json",
                "User-Agent": "AEGIS-Emergency-Guidance/1.0 (educational prototype)",
            },
            timeout=timeout,
        )
    except requests.exceptions.Timeout:
        return {
            "success": False,
            "error": "SACHET official alert request timed out. Retry manually later.",
            "checked_at_utc": checked_at,
        }
    except requests.exceptions.RequestException as exc:
        return {
            "success": False,
            "error": f"SACHET official alert network error: {exc}",
            "checked_at_utc": checked_at,
        }

    if response.status_code != 200:
        return {
            "success": False,
            "error": f"SACHET official alert service returned HTTP {response.status_code}.",
            "checked_at_utc": checked_at,
        }

    try:
        payload = response.json()
    except ValueError:
        return {
            "success": False,
            "error": "SACHET official alert service returned invalid JSON.",
            "checked_at_utc": checked_at,
        }
    if not isinstance(payload, dict) or not isinstance(payload.get("alerts"), list):
        return {
            "success": False,
            "error": "SACHET official alert service returned an unexpected response format.",
            "checked_at_utc": checked_at,
        }

    alerts_by_id = {}
    malformed_count = 0
    for raw_alert in payload["alerts"]:
        alert = _normalize_alert(raw_alert)
        if alert is None:
            malformed_count += 1
            continue
        alerts_by_id.setdefault(alert["id"], alert)

    report = {
        "success": True,
        "source": "NDMA SACHET National Disaster Alert Portal",
        "source_url": SACHET_ALERT_PORTAL_URL,
        "endpoint": SACHET_LOCATION_ALERTS_URL,
        "search_radius_km": SACHET_ALERT_RADIUS_KM,
        "latitude": lat,
        "longitude": lon,
        "retrieved_at_utc": checked_at,
        "alerts": list(alerts_by_id.values()),
        "malformed_alert_count": malformed_count,
    }
    with _CACHE_LOCK:
        _SUCCESS_CACHE[cache_key] = (
            time.monotonic() + ALERT_CACHE_TTL_SECONDS,
            deepcopy(report),
        )
    return report


def report_is_stale(
    report: object,
    now: Optional[datetime] = None,
    max_age_seconds: int = ALERT_STALE_AFTER_SECONDS,
) -> bool:
    if not isinstance(report, dict) or not report.get("success"):
        return True
    retrieved = _parse_datetime(report.get("retrieved_at_utc"))
    if retrieved is None:
        return True
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    age = (current - retrieved).total_seconds()
    return age < 0 or age > max_age_seconds


def classify_alert_report(
    report: object,
    now: Optional[datetime] = None,
) -> Dict:
    """Summarize source and validity state without treating missing times as active."""
    empty_counts = {
        "active_count": 0,
        "scheduled_count": 0,
        "expired_count": 0,
        "validity_unknown_count": 0,
    }
    if (
        not isinstance(report, dict)
        or report.get("success") is not True
        or not isinstance(report.get("alerts"), list)
    ):
        return {"status": "unavailable", **empty_counts}

    counts = dict(empty_counts)
    for alert in report["alerts"]:
        if not isinstance(alert, dict):
            continue
        state = alert_time_state(alert, now)
        if state == "current":
            counts["active_count"] += 1
        elif state == "scheduled":
            counts["scheduled_count"] += 1
        elif state == "expired":
            counts["expired_count"] += 1
        else:
            counts["validity_unknown_count"] += 1

    if report_is_stale(report, now):
        status = "stale"
    elif counts["active_count"]:
        status = "active"
    elif counts["scheduled_count"]:
        status = "scheduled"
    elif counts["validity_unknown_count"]:
        status = "validity_unknown"
    elif counts["expired_count"]:
        status = "expired"
    else:
        status = "no_matching_alerts"
    return {"status": status, **counts}


def alert_covers_point(
    alert: Dict,
    latitude: float,
    longitude: float,
) -> Optional[bool]:
    """Return whether the source polygon covers a point; None means unknown."""
    geometry = alert.get("geometry")
    if not isinstance(geometry, dict) or not _valid_coordinates(latitude, longitude):
        return None
    lat, lon = float(latitude), float(longitude)
    polygons = (
        [geometry["coordinates"]]
        if geometry.get("type") == "Polygon"
        else geometry.get("coordinates", [])
    )
    for polygon in polygons:
        if _polygon_covers_point(polygon, lon, lat):
            return True
    return False


def _polygon_covers_point(polygon: list, x: float, y: float) -> bool:
    if not polygon or not _ring_covers_point(polygon[0], x, y):
        return False
    for hole in polygon[1:]:
        if _ring_contains_point(hole, x, y):
            return False
    return True


def _ring_contains_point(ring: list, x: float, y: float) -> bool:
    inside = False
    for index in range(len(ring) - 1):
        x1, y1 = float(ring[index][0]), float(ring[index][1])
        x2, y2 = float(ring[index + 1][0]), float(ring[index + 1][1])
        if (y1 > y) != (y2 > y):
            crossing_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < crossing_x:
                inside = not inside
    return inside


def _ring_covers_point(ring: list, x: float, y: float) -> bool:
    for index in range(len(ring) - 1):
        if _point_on_segment(
            x,
            y,
            float(ring[index][0]),
            float(ring[index][1]),
            float(ring[index + 1][0]),
            float(ring[index + 1][1]),
        ):
            return True
    return _ring_contains_point(ring, x, y)


def _point_on_segment(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> bool:
    cross = (px - ax) * (by - ay) - (py - ay) * (bx - ax)
    if abs(cross) > 1e-10:
        return False
    return (
        min(ax, bx) - 1e-10 <= px <= max(ax, bx) + 1e-10
        and min(ay, by) - 1e-10 <= py <= max(ay, by) + 1e-10
    )


def _segments_intersect(
    ax: float,
    ay: float,
    bx: float,
    by: float,
    cx: float,
    cy: float,
    dx: float,
    dy: float,
) -> bool:
    def orientation(px: float, py: float, qx: float, qy: float, rx: float, ry: float) -> float:
        return (qy - py) * (rx - qx) - (qx - px) * (ry - qy)

    o1 = orientation(ax, ay, bx, by, cx, cy)
    o2 = orientation(ax, ay, bx, by, dx, dy)
    o3 = orientation(cx, cy, dx, dy, ax, ay)
    o4 = orientation(cx, cy, dx, dy, bx, by)
    if (
        ((o1 > 0 and o2 < 0) or (o1 < 0 and o2 > 0))
        and ((o3 > 0 and o4 < 0) or (o3 < 0 and o4 > 0))
    ):
        return True
    return (
        (abs(o1) <= 1e-10 and _point_on_segment(cx, cy, ax, ay, bx, by))
        or (abs(o2) <= 1e-10 and _point_on_segment(dx, dy, ax, ay, bx, by))
        or (abs(o3) <= 1e-10 and _point_on_segment(ax, ay, cx, cy, dx, dy))
        or (abs(o4) <= 1e-10 and _point_on_segment(bx, by, cx, cy, dx, dy))
    )


def route_intersects_alert(
    alert: Dict,
    route_coordinates: object,
) -> Optional[bool]:
    """Return exact sampled-polyline/polygon intersection, or None without geometry."""
    geometry = alert.get("geometry")
    if not isinstance(geometry, dict):
        return None
    if not isinstance(route_coordinates, list) or not route_coordinates:
        return None
    normalized_route = []
    for point in route_coordinates:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            return None
        try:
            lat, lon = float(point[0]), float(point[1])
        except (TypeError, ValueError):
            return None
        if not _valid_coordinates(lat, lon):
            return None
        normalized_route.append((lon, lat))

    if any(
        alert_covers_point(alert, lat, lon) is True
        for lon, lat in normalized_route
    ):
        return True
    if len(normalized_route) == 1:
        return False

    polygons = (
        [geometry["coordinates"]]
        if geometry.get("type") == "Polygon"
        else geometry.get("coordinates", [])
    )
    for route_start, route_end in zip(normalized_route, normalized_route[1:]):
        for polygon in polygons:
            for ring in polygon:
                for edge_start, edge_end in zip(ring, ring[1:]):
                    if _segments_intersect(
                        route_start[0],
                        route_start[1],
                        route_end[0],
                        route_end[1],
                        float(edge_start[0]),
                        float(edge_start[1]),
                        float(edge_end[0]),
                        float(edge_end[1]),
                    ):
                        return True
    return False


def assess_route_alert_overlaps(
    routes: Dict,
    alerts: object,
) -> Dict[str, Dict]:
    """Compare real route geometry with fresh, source-provided alert polygons.

    A non-intersection is limited to the returned polygons; it is not clearance
    because the source query may not cover the full route corridor.
    """
    usable_alerts = [
        alert
        for alert in alerts
        if isinstance(alert, dict) and isinstance(alert.get("geometry"), dict)
    ] if isinstance(alerts, list) else []
    assessments = {}
    for route_id, route in routes.items():
        route_coordinates = route.get("route_coords") if isinstance(route, dict) else None
        checks = [
            (alert, route_intersects_alert(alert, route_coordinates))
            for alert in usable_alerts
        ]
        overlaps = [
            alert for alert, intersects in checks if intersects is True
        ]
        retrieved_times = sorted({
            alert["_source_retrieved_at_utc"]
            for alert in usable_alerts
            if isinstance(alert.get("_source_retrieved_at_utc"), str)
        })
        if overlaps:
            status = "overlaps_returned_alert_area"
        elif checks and all(intersects is False for _, intersects in checks):
            status = "no_overlap_with_returned_alert_areas"
        else:
            status = "unknown"
        assessments[route_id] = {
            "status": status,
            "alerts": [
                {
                    "hazard_type": alert.get("hazard_type", "Official alert"),
                    "severity": alert.get("severity", "Not supplied by source"),
                    "issuing_authority": alert.get(
                        "issuing_authority", "Not supplied by source"
                    ),
                    "id": alert.get("id"),
                }
                for alert in overlaps
            ],
            "source": "NDMA SACHET",
            "retrieved_at_utc": retrieved_times,
            "geometry_checks": len(checks),
            "coverage_note": (
                "Checks cover only polygons returned for selected-location queries; "
                "full route-corridor coverage is not established."
            ),
        }
    return assessments
