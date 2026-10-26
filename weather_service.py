"""Open-Meteo forecast retrieval for real-world AEGIS locations."""

from datetime import datetime, timezone
import math
from typing import Dict, List, Optional

import requests


OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
WEATHER_TIMEOUT_SECONDS = 10
FORECAST_HOURS = 6

_WEATHER_CODES = {
    0: ("Clear sky", "☀️"),
    1: ("Mainly clear", "🌤️"),
    2: ("Partly cloudy", "⛅"),
    3: ("Overcast", "☁️"),
    45: ("Fog", "🌫️"),
    48: ("Depositing rime fog", "🌫️"),
    51: ("Light drizzle", "🌦️"),
    53: ("Moderate drizzle", "🌦️"),
    55: ("Dense drizzle", "🌧️"),
    56: ("Light freezing drizzle", "🌧️"),
    57: ("Dense freezing drizzle", "🌧️"),
    61: ("Slight rain", "🌧️"),
    63: ("Moderate rain", "🌧️"),
    65: ("Heavy rain", "🌧️"),
    66: ("Light freezing rain", "🌧️"),
    67: ("Heavy freezing rain", "🌧️"),
    71: ("Slight snowfall", "🌨️"),
    73: ("Moderate snowfall", "🌨️"),
    75: ("Heavy snowfall", "❄️"),
    77: ("Snow grains", "🌨️"),
    80: ("Slight rain showers", "🌦️"),
    81: ("Moderate rain showers", "🌧️"),
    82: ("Violent rain showers", "⛈️"),
    85: ("Slight snow showers", "🌨️"),
    86: ("Heavy snow showers", "❄️"),
    95: ("Thunderstorm", "⛈️"),
    96: ("Thunderstorm with slight hail", "⛈️"),
    99: ("Thunderstorm with heavy hail", "⛈️"),
}


def _valid_coordinates(latitude: float, longitude: float) -> bool:
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


def _weather_description(code: object) -> tuple:
    try:
        numeric_code = int(code)
    except (TypeError, ValueError):
        return "Weather description unavailable", "🌡️"
    return _WEATHER_CODES.get(numeric_code, ("Unknown weather code", "🌡️"))


def _numeric_value(value: object):
    if value is None or isinstance(value, bool):
        return None
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return None
    return numeric_value if math.isfinite(numeric_value) else None


def _hourly_forecast(data: object, current_time: Optional[str]) -> List[Dict]:
    if not isinstance(data, dict):
        return []

    times = data.get("time")
    if not isinstance(times, list):
        return []

    current_dt = None
    if isinstance(current_time, str):
        try:
            current_dt = datetime.fromisoformat(current_time)
        except ValueError:
            current_dt = None

    result = []
    for index, timestamp in enumerate(times):
        if not isinstance(timestamp, str):
            continue
        try:
            timestamp_dt = datetime.fromisoformat(timestamp)
        except ValueError:
            continue
        if current_dt is not None:
            if timestamp_dt < current_dt:
                continue

        def value(field: str):
            values = data.get(field)
            raw_value = (
                values[index]
                if isinstance(values, list) and index < len(values)
                else None
            )
            return _numeric_value(raw_value)

        code = value("weather_code")
        if code is not None:
            code = int(code)
        description, icon = _weather_description(code)
        result.append({
            "time": timestamp,
            "temperature_c": value("temperature_2m"),
            "precipitation_probability_percent": value("precipitation_probability"),
            "precipitation_mm": value("precipitation"),
            "rain_mm": value("rain"),
            "wind_speed_kmh": value("wind_speed_10m"),
            "weather_code": code,
            "weather_description": description,
            "icon": icon,
        })
        if len(result) >= FORECAST_HOURS:
            break
    return result


def get_weather(
    latitude: float,
    longitude: float,
    timeout: int = WEATHER_TIMEOUT_SECONDS,
) -> Dict:
    """Return current and near-term hourly weather for valid coordinates.

    Errors are returned as ``{"success": False, "error": ...}`` so callers can
    display them without interrupting route planning.
    """
    checked_at = datetime.now(timezone.utc).isoformat()
    if not _valid_coordinates(latitude, longitude):
        return {
            "success": False,
            "error": "Weather requires valid latitude and longitude coordinates.",
            "checked_at_utc": checked_at,
        }

    lat = float(latitude)
    lon = float(longitude)
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": (
            "temperature_2m,relative_humidity_2m,apparent_temperature,"
            "is_day,precipitation,rain,weather_code,wind_speed_10m,wind_direction_10m"
        ),
        "hourly": (
            "temperature_2m,precipitation_probability,precipitation,rain,"
            "wind_speed_10m,weather_code"
        ),
        "forecast_days": 2,
        "timezone": "auto",
    }

    try:
        response = requests.get(
            OPEN_METEO_FORECAST_URL,
            params=params,
            timeout=timeout,
        )
    except requests.exceptions.Timeout:
        return {
            "success": False,
            "error": "Weather request timed out. Please try again later.",
            "checked_at_utc": checked_at,
        }
    except requests.exceptions.RequestException as exc:
        return {
            "success": False,
            "error": f"Weather service network error: {exc}",
            "checked_at_utc": checked_at,
        }

    if response.status_code != 200:
        return {
            "success": False,
            "error": f"Open-Meteo returned HTTP {response.status_code}.",
            "checked_at_utc": checked_at,
        }

    try:
        payload = response.json()
    except ValueError:
        return {
            "success": False,
            "error": "Open-Meteo returned an invalid JSON response.",
            "checked_at_utc": checked_at,
        }

    if not isinstance(payload, dict):
        return {
            "success": False,
            "error": "Open-Meteo returned an unexpected response format.",
            "checked_at_utc": checked_at,
        }
    if payload.get("error"):
        reason = payload.get("reason")
        return {
            "success": False,
            "error": f"Open-Meteo could not provide a forecast: {reason or 'API error'}.",
            "checked_at_utc": checked_at,
        }

    raw_current = payload.get("current")
    current = raw_current if isinstance(raw_current, dict) else {}
    weather_code = _numeric_value(current.get("weather_code"))
    if weather_code is not None:
        weather_code = int(weather_code)
    description, icon = _weather_description(weather_code)
    current_data = {
        "time": current.get("time"),
        "temperature_c": _numeric_value(current.get("temperature_2m")),
        "apparent_temperature_c": _numeric_value(current.get("apparent_temperature")),
        "relative_humidity_percent": _numeric_value(current.get("relative_humidity_2m")),
        "precipitation_mm": _numeric_value(current.get("precipitation")),
        "rain_mm": _numeric_value(current.get("rain")),
        "wind_speed_kmh": _numeric_value(current.get("wind_speed_10m")),
        "wind_direction_degrees": _numeric_value(current.get("wind_direction_10m")),
        "weather_code": weather_code,
        "weather_description": description,
        "icon": icon,
    }
    hourly = _hourly_forecast(payload.get("hourly"), current_data["time"])
    has_current_measurements = any(
        current_data[key] is not None
        for key in (
            "temperature_c",
            "weather_code",
            "precipitation_mm",
            "wind_speed_kmh",
        )
    )
    has_hourly_measurements = any(
        any(
            item[key] is not None
            for key in (
                "temperature_c",
                "precipitation_probability_percent",
                "precipitation_mm",
                "wind_speed_kmh",
                "weather_code",
            )
        )
        for item in hourly
    )
    if not has_current_measurements and not has_hourly_measurements:
        return {
            "success": False,
            "error": (
                "Weather forecast unavailable: the provider returned no usable "
                "current or hourly measurements for these coordinates."
            ),
            "checked_at_utc": checked_at,
        }

    warnings = []
    if not current:
        warnings.append("Current conditions were not included in the response.")
    elif all(
        current_data[key] is None
        for key in ("temperature_c", "weather_code", "precipitation_mm", "wind_speed_kmh")
    ):
        warnings.append("Current weather measurements were missing from the response.")
    if not hourly:
        warnings.append("Hourly forecast data were not included in the response.")

    return {
        "success": True,
        "latitude": payload.get("latitude", lat),
        "longitude": payload.get("longitude", lon),
        "timezone": payload.get("timezone"),
        "timezone_abbreviation": payload.get("timezone_abbreviation"),
        "current": current_data,
        "hourly": hourly,
        "warnings": warnings,
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
    }
