"""Forecast-based emergency indicators for selected real-world locations."""

from datetime import date, datetime, timezone
import hashlib
import math
from typing import Dict, Optional

import requests


OPEN_METEO_FLOOD_URL = "https://flood-api.open-meteo.com/v1/flood"
FLOOD_TIMEOUT_SECONDS = 12
DEFAULT_RAINFALL_SCREENING_THRESHOLD_MM = 10.0
MAX_REPORT_AGE_SECONDS = 30 * 60


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


def _retrieval_time() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_flood_forecast(
    latitude: float,
    longitude: float,
    timeout: int = FLOOD_TIMEOUT_SECONDS,
) -> Dict:
    """Retrieve GloFAS-derived daily river discharge forecast values."""
    checked_at = _retrieval_time()
    if not _valid_coordinates(latitude, longitude):
        return {
            "success": False,
            "error": "Flood forecast requires valid latitude and longitude coordinates.",
            "checked_at_utc": checked_at,
        }

    lat = float(latitude)
    lon = float(longitude)
    try:
        response = requests.get(
            OPEN_METEO_FLOOD_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": "river_discharge",
                "forecast_days": 7,
                "timezone": "auto",
            },
            timeout=timeout,
        )
    except requests.exceptions.Timeout:
        return {
            "success": False,
            "error": "Flood forecast request timed out. Please try again later.",
            "checked_at_utc": checked_at,
        }
    except requests.exceptions.RequestException as exc:
        return {
            "success": False,
            "error": f"Flood forecast network error: {exc}",
            "checked_at_utc": checked_at,
        }

    if response.status_code != 200:
        return {
            "success": False,
            "error": f"Open-Meteo Flood API returned HTTP {response.status_code}.",
            "checked_at_utc": checked_at,
        }

    try:
        payload = response.json()
    except ValueError:
        return {
            "success": False,
            "error": "Open-Meteo Flood API returned invalid JSON.",
            "checked_at_utc": checked_at,
        }

    if not isinstance(payload, dict):
        return {
            "success": False,
            "error": "Open-Meteo Flood API returned an unexpected response format.",
            "checked_at_utc": checked_at,
        }
    if payload.get("error"):
        return {
            "success": False,
            "error": (
                "Open-Meteo Flood API could not provide a forecast: "
                f"{payload.get('reason') or 'API error'}."
            ),
            "checked_at_utc": checked_at,
        }

    daily = payload.get("daily")
    daily = daily if isinstance(daily, dict) else {}
    times = daily.get("time")
    values = daily.get("river_discharge")
    records = []
    if isinstance(times, list) and isinstance(values, list):
        for day, value in zip(times, values):
            if not isinstance(day, str):
                continue
            try:
                date.fromisoformat(day)
            except ValueError:
                continue
            try:
                discharge = float(value)
            except (TypeError, ValueError):
                continue
            if discharge is not None and (
                math.isfinite(discharge) and discharge >= 0
            ):
                records.append({
                    "date": day,
                    "river_discharge_m3s": discharge,
                })

    warnings = []
    if not records:
        return {
            "success": False,
            "error": (
                "Flood forecast unavailable: the provider returned no usable "
                "daily river-discharge values for these coordinates."
            ),
            "checked_at_utc": checked_at,
        }
    if len(records) < min(len(times), len(values)):
        warnings.append(
            "Some flood forecast rows had invalid dates or discharge values and were omitted."
        )

    return {
        "success": True,
        "latitude": payload.get("latitude", lat),
        "longitude": payload.get("longitude", lon),
        "timezone": payload.get("timezone"),
        "retrieved_at_utc": checked_at,
        "daily": records,
        "warnings": warnings,
    }


def _parse_timestamp(value: object) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _has_timezone(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _report_is_fresh(report: object, now: datetime) -> bool:
    if not isinstance(report, dict) or not report.get("success"):
        return False
    updated_at = _parse_timestamp(
        report.get("retrieved_at_utc") or report.get("checked_at_utc")
    )
    if updated_at is None:
        return False
    age = (now - updated_at).total_seconds()
    return 0 <= age <= MAX_REPORT_AGE_SECONDS


def assess_emergency_conditions(
    weather_report: object,
    flood_report: object,
    rainfall_threshold_mm: float = DEFAULT_RAINFALL_SCREENING_THRESHOLD_MM,
    now: Optional[datetime] = None,
) -> Dict:
    """Assess only forecast indicators; never infer a safe or normal status."""
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)

    try:
        threshold = float(rainfall_threshold_mm)
    except (TypeError, ValueError) as exc:
        raise ValueError("Rainfall screening trigger must be a positive number.") from exc
    if not math.isfinite(threshold) or threshold <= 0:
        raise ValueError("Rainfall screening trigger must be a positive number.")

    weather_fresh = _report_is_fresh(weather_report, now_utc)
    flood_fresh = _report_is_fresh(flood_report, now_utc)
    alerts = []
    if weather_fresh and isinstance(weather_report, dict):
        weather_current = weather_report.get("current")
        current_time = (
            weather_current.get("time")
            if isinstance(weather_current, dict)
            else None
        )
        current_timestamp = _parse_timestamp(current_time)
        hourly = weather_report.get("hourly")
        if isinstance(hourly, list):
            for item in hourly:
                if not isinstance(item, dict):
                    continue
                forecast_time = item.get("time")
                parsed_forecast_time = _parse_timestamp(forecast_time)
                if (
                    parsed_forecast_time is None
                    or (
                        _has_timezone(forecast_time)
                        and parsed_forecast_time < now_utc
                    )
                    or (
                        current_timestamp is not None
                        and parsed_forecast_time <= current_timestamp
                    )
                ):
                    continue
                try:
                    rainfall = float(item.get("rain_mm"))
                except (TypeError, ValueError):
                    continue
                if math.isfinite(rainfall) and rainfall >= threshold:
                    alerts.append({
                        "hazard": "Heavy-rain forecast indicator",
                        "severity": "Watch",
                        "certainty": "Not supplied by source; no probability estimated",
                        "timing": forecast_time,
                        "rainfall_mm": rainfall,
                        "source": "Open-Meteo forecast",
                        "data_kind": "Model forecast, not a local rain-gauge observation",
                        "threshold_mm": threshold,
                    })

    return {
        "status": "Watch" if alerts else "Unknown",
        "status_reason": (
            (
                f"Forecast hourly rainfall met the AEGIS screening trigger "
                f"of {threshold:g} mm."
            )
            if alerts
            else (
                "Weather and flood forecast data are unavailable, stale, or "
                "insufficient. DHM warnings are not connected; this is not "
                "evidence that the area is safe."
                if not weather_fresh and not flood_fresh
                else (
                    "No applicable official warning feed or locally validated "
                    "rainfall/river threshold is connected. Missing or "
                    "below-trigger indicators do not mean the area is safe."
                )
            )
        ),
        "alerts": alerts,
        "weather_fresh": weather_fresh,
        "flood_fresh": flood_fresh,
        "official_warning_feed_connected": False,
        "checked_at_utc": now_utc.isoformat(),
    }


def emergency_alert_id(
    alert: Dict,
    latitude: float,
    longitude: float,
) -> str:
    """Return a stable identifier so repeated refreshes do not reannounce an alert."""
    identity = "|".join((
        str(alert.get("hazard", "")),
        f"{float(latitude):.5f}",
        f"{float(longitude):.5f}",
        str(alert.get("timing", "")),
    ))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def reconcile_active_alerts(
    previous: Dict[str, Dict],
    current: Dict[str, Dict],
) -> Dict:
    """Identify newly active and cleared forecast indicators by stable ID."""
    return {
        "new": [
            alert for alert_id, alert in current.items()
            if alert_id not in previous
        ],
        "cleared": [
            alert for alert_id, alert in previous.items()
            if alert_id not in current
        ],
    }


def last_successful_report(
    current: object,
    previous: object,
) -> Optional[Dict]:
    """Keep the latest successful provider response across failed refreshes."""
    if isinstance(current, dict) and current.get("success"):
        return current
    if isinstance(previous, dict) and previous.get("success"):
        return previous
    return None
