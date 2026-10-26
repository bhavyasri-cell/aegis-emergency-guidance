import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import Mock, patch

import requests

from weather_service import clear_weather_cache, get_weather


class WeatherServiceTests(unittest.TestCase):
    def _response(self, payload, status_code=200):
        response = Mock()
        response.status_code = status_code
        response.json.return_value = payload
        response.headers = {}
        return response

    def setUp(self):
        clear_weather_cache()

    @patch("weather_service.requests.get")
    def test_valid_coordinates_return_current_and_hourly_weather(self, get):
        get.return_value = self._response({
            "latitude": 28.6,
            "longitude": 77.2,
            "timezone": "Asia/Kolkata",
            "timezone_abbreviation": "IST",
            "current": {
                "time": "2026-10-21T12:00",
                "temperature_2m": 29.4,
                "relative_humidity_2m": 55,
                "apparent_temperature": 31.0,
                "precipitation": 0.2,
                "rain": 0.1,
                "weather_code": 2,
                "wind_speed_10m": 12.3,
                "wind_direction_10m": 180,
            },
            "hourly": {
                "time": [
                    "2026-10-21T11:00",
                    "2026-10-21T12:00",
                    "2026-10-21T13:00",
                    "2026-10-21T14:00",
                ],
                "temperature_2m": [28, 29, 30, 29],
                "precipitation_probability": [10, 20, 30, 40],
                "precipitation": [0, 0.2, 1, 0],
                "rain": [0, 0.1, 1, 0],
                "wind_speed_10m": [10, 12, 15, 11],
                "weather_code": [1, 2, 61, 3],
            },
        })

        report = get_weather(28.6, 77.2)

        self.assertTrue(report["success"])
        self.assertEqual(report["current"]["temperature_c"], 29.4)
        self.assertEqual(report["current"]["weather_description"], "Partly cloudy")
        self.assertEqual(report["current"]["rain_mm"], 0.1)
        retrieved_at = datetime.fromisoformat(
            report["retrieved_at_utc"].replace("Z", "+00:00")
        )
        self.assertLessEqual(
            abs((datetime.now(timezone.utc) - retrieved_at).total_seconds()),
            5,
        )
        self.assertEqual(len(report["hourly"]), 3)
        self.assertEqual(report["hourly"][0]["time"], "2026-10-21T12:00")
        request_params = get.call_args.kwargs["params"]
        self.assertEqual(request_params["latitude"], 28.6)
        self.assertEqual(request_params["longitude"], 77.2)
        self.assertEqual(request_params["timezone"], "auto")

    @patch("weather_service.requests.get")
    def test_different_coordinates_are_sent_to_weather_api(self, get):
        get.return_value = self._response({"current": {}, "hourly": {}})

        get_weather(28.6, 77.2)
        get_weather(40.7, -74.0)

        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args_list[0].kwargs["params"]["latitude"], 28.6)
        self.assertEqual(get.call_args_list[1].kwargs["params"]["latitude"], 40.7)
        self.assertEqual(get.call_args_list[1].kwargs["params"]["longitude"], -74.0)

    @patch("weather_service.requests.get")
    def test_invalid_coordinates_do_not_make_a_request(self, get):
        for coordinates in ((91, 0), (0, 181), (float("nan"), 0), ("invalid", 0)):
            with self.subTest(coordinates=coordinates):
                report = get_weather(*coordinates)
                self.assertFalse(report["success"])
                self.assertIn("valid latitude", report["error"])
                self.assertIn("checked_at_utc", report)
                self.assertNotIn("retrieved_at_utc", report)
        get.assert_not_called()

    @patch("weather_service.requests.get")
    def test_http_api_failure_is_returned_to_caller(self, get):
        get.return_value = self._response({}, status_code=503)

        report = get_weather(28.6, 77.2)

        self.assertFalse(report["success"])
        self.assertIn("HTTP 503", report["error"])

    @patch("weather_service.requests.get")
    def test_http_429_honors_retry_after_and_blocks_manual_retry(self, get):
        response = self._response({}, status_code=429)
        response.headers = {"Retry-After": "120"}
        get.return_value = response

        first_report = get_weather(28.6, 77.2)
        retry_report = get_weather(28.6, 77.2, force_refresh=True)

        self.assertFalse(first_report["success"])
        self.assertIn("rate limit", first_report["error"])
        self.assertAlmostEqual(first_report["retry_after_seconds"], 120, delta=1)
        self.assertFalse(retry_report["success"])
        self.assertGreater(retry_report["retry_after_seconds"], 0)
        get.assert_called_once()

    @patch("weather_service.requests.get")
    def test_http_429_retry_after_http_date_is_parsed(self, get):
        retry_time = datetime.now(timezone.utc) + timedelta(minutes=2)
        response = self._response({}, status_code=429)
        response.headers = {"Retry-After": format_datetime(retry_time, usegmt=True)}
        get.return_value = response

        report = get_weather(28.6, 77.2)

        self.assertTrue(115 <= report["retry_after_seconds"] <= 120)

    @patch("weather_service.requests.get")
    def test_refresh_failure_preserves_last_success_as_explicit_cached_data(self, get):
        successful_response = self._response({
            "current": {
                "time": "2026-10-21T12:00",
                "temperature_2m": 29,
            },
        })
        limited_response = self._response({}, status_code=429)
        limited_response.headers = {"Retry-After": "90"}
        get.side_effect = [successful_response, limited_response]

        original = get_weather(28.6, 77.2)
        refreshed = get_weather(28.6, 77.2, force_refresh=True)

        self.assertTrue(original["success"])
        self.assertTrue(refreshed["success"])
        self.assertFalse(refreshed["is_stale"])
        self.assertEqual(refreshed["cache_status"], "cached")
        self.assertIn("HTTP 429", refreshed["last_refresh_error"])
        self.assertEqual(
            refreshed["retrieved_at_utc"],
            original["retrieved_at_utc"],
        )

    @patch("weather_service.requests.get")
    def test_failed_refresh_marks_expired_success_as_stale(self, get):
        successful_response = self._response({
            "current": {
                "time": "2026-10-21T12:00",
                "temperature_2m": 29,
            },
        })
        limited_response = self._response({}, status_code=429)
        limited_response.headers = {"Retry-After": "90"}
        get.side_effect = [successful_response, limited_response]

        with patch("weather_service.WEATHER_CACHE_TTL_SECONDS", 0):
            get_weather(28.6, 77.2)
            refreshed = get_weather(28.6, 77.2, force_refresh=True)

        self.assertTrue(refreshed["success"])
        self.assertTrue(refreshed["is_stale"])
        self.assertEqual(refreshed["cache_status"], "stale")
        self.assertIn("HTTP 429", refreshed["last_refresh_error"])

    @patch("weather_service.requests.get")
    def test_backoff_is_bounded_and_service_recovers_after_cooldown(self, get):
        get.side_effect = [
            self._response({}, status_code=503),
            self._response({
                "current": {
                    "time": "2026-10-21T12:00",
                    "temperature_2m": 29,
                },
            }),
        ]

        with patch("weather_service.time.monotonic", side_effect=[100.0, 131.0]):
            failed = get_weather(28.6, 77.2)
            recovered = get_weather(28.6, 77.2)

        self.assertFalse(failed["success"])
        self.assertEqual(failed["retry_after_seconds"], 30)
        self.assertTrue(recovered["success"])
        self.assertEqual(get.call_count, 2)

    @patch("weather_service.requests.get")
    def test_successful_forecast_is_reused_for_same_coordinates(self, get):
        get.return_value = self._response({
            "current": {
                "time": "2026-10-21T12:00",
                "temperature_2m": 29,
            },
        })

        first = get_weather(28.6, 77.2)
        second = get_weather(28.6, 77.2)

        self.assertTrue(first["success"])
        self.assertTrue(second["success"])
        self.assertEqual(second["cache_status"], "cached")
        get.assert_called_once()

    @patch("weather_service.requests.get")
    def test_timeout_is_reported_gracefully(self, get):
        get.side_effect = requests.exceptions.Timeout()

        report = get_weather(28.6, 77.2)

        self.assertFalse(report["success"])
        self.assertIn("timed out", report["error"])
        self.assertIn("checked_at_utc", report)
        self.assertNotIn("retrieved_at_utc", report)

    @patch("weather_service.requests.get")
    def test_connection_error_is_reported_without_claiming_a_retrieval(self, get):
        get.side_effect = requests.exceptions.ConnectionError("offline")

        report = get_weather(28.6, 77.2)

        self.assertFalse(report["success"])
        self.assertIn("network error", report["error"])
        self.assertIn("checked_at_utc", report)
        self.assertNotIn("retrieved_at_utc", report)

    @patch("weather_service.requests.get")
    def test_missing_weather_data_is_reported_as_unavailable(self, get):
        get.return_value = self._response({
            "latitude": 28.6,
            "longitude": 77.2,
        })

        report = get_weather(28.6, 77.2)

        self.assertFalse(report["success"])
        self.assertIn("no usable", report["error"])
        self.assertIn("checked_at_utc", report)
        self.assertNotIn("retrieved_at_utc", report)

    @patch("weather_service.requests.get")
    def test_malformed_measurements_are_not_reported_as_live_values(self, get):
        get.return_value = self._response({
            "current": {
                "time": "2026-10-21T12:00",
                "temperature_2m": "not a number",
                "weather_code": None,
                "precipitation": float("nan"),
                "wind_speed_10m": None,
            },
            "hourly": {
                "time": ["not a timestamp"],
                "temperature_2m": [30],
            },
        })

        report = get_weather(28.6, 77.2)

        self.assertFalse(report["success"])
        self.assertIn("no usable", report["error"])

    @patch("weather_service.requests.get")
    def test_open_meteo_api_error_is_reported(self, get):
        get.return_value = self._response({
            "error": True,
            "reason": "Invalid parameter",
        })

        report = get_weather(28.6, 77.2)

        self.assertFalse(report["success"])
        self.assertIn("Invalid parameter", report["error"])

    @patch("weather_service.requests.get")
    def test_invalid_json_is_reported_and_throttled(self, get):
        response = self._response({})
        response.json.side_effect = ValueError("invalid JSON")
        get.return_value = response

        first = get_weather(28.6, 77.2)
        second = get_weather(28.6, 77.2, force_refresh=True)

        self.assertFalse(first["success"])
        self.assertIn("invalid JSON", first["error"])
        self.assertFalse(second["success"])
        get.assert_called_once()


if __name__ == "__main__":
    unittest.main()
