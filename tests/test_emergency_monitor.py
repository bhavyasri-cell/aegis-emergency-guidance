import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import requests

from emergency_monitor import (
    DEFAULT_RAINFALL_SCREENING_THRESHOLD_MM,
    assess_emergency_conditions,
    emergency_alert_id,
    get_flood_forecast,
    last_successful_report,
    reconcile_active_alerts,
)


class EmergencyMonitorTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 21, 12, 30, tzinfo=timezone.utc)

    @staticmethod
    def _response(payload, status_code=200):
        response = Mock()
        response.status_code = status_code
        response.json.return_value = payload
        return response

    def _weather(self, hourly, retrieved_at=None):
        return {
            "success": True,
            "retrieved_at_utc": (
                retrieved_at or self.now
            ).isoformat(),
            "current": {"time": "2026-10-21T12:00"},
            "hourly": hourly,
        }

    def _flood(self, daily=None, retrieved_at=None):
        return {
            "success": True,
            "retrieved_at_utc": (
                retrieved_at or self.now
            ).isoformat(),
            "daily": daily or [],
        }

    @patch("emergency_monitor.requests.get")
    def test_flood_api_returns_daily_discharge_and_grid_coordinates(self, get):
        get.return_value = self._response({
            "latitude": 28.625,
            "longitude": 77.225,
            "timezone": "Asia/Kolkata",
            "daily": {
                "time": ["2026-10-21", "2026-10-22"],
                "river_discharge": [324.84, 312.33],
            },
        })

        report = get_flood_forecast(28.63177, 77.21938)

        self.assertTrue(report["success"])
        self.assertEqual(report["latitude"], 28.625)
        self.assertEqual(report["daily"][0]["river_discharge_m3s"], 324.84)
        self.assertEqual(report["daily"][1]["date"], "2026-10-22")
        self.assertTrue(report["retrieved_at_utc"])
        params = get.call_args.kwargs["params"]
        self.assertEqual(params["latitude"], 28.63177)
        self.assertEqual(params["longitude"], 77.21938)
        self.assertEqual(params["daily"], "river_discharge")
        self.assertEqual(params["forecast_days"], 7)

    @patch("emergency_monitor.requests.get")
    def test_invalid_coordinates_are_rejected_without_network_request(self, get):
        for coordinates in ((91, 0), (0, 181), (float("nan"), 0), ("bad", 0)):
            with self.subTest(coordinates=coordinates):
                report = get_flood_forecast(*coordinates)
                self.assertFalse(report["success"])
                self.assertIn("valid latitude", report["error"])
        get.assert_not_called()

    @patch("emergency_monitor.requests.get")
    def test_timeout_and_http_failures_are_reported(self, get):
        get.side_effect = requests.exceptions.Timeout()
        timed_out = get_flood_forecast(28.6, 77.2)
        self.assertFalse(timed_out["success"])
        self.assertIn("timed out", timed_out["error"])

        get.side_effect = None
        get.return_value = self._response({}, status_code=503)
        unavailable = get_flood_forecast(28.6, 77.2)
        self.assertFalse(unavailable["success"])
        self.assertIn("HTTP 503", unavailable["error"])

    @patch("emergency_monitor.requests.get")
    def test_network_failure_is_reported_without_crashing(self, get):
        get.side_effect = requests.exceptions.ConnectionError("offline")

        report = get_flood_forecast(28.6, 77.2)

        self.assertFalse(report["success"])
        self.assertIn("network error", report["error"])
        self.assertIn("checked_at_utc", report)
        self.assertNotIn("retrieved_at_utc", report)

    @patch("emergency_monitor.requests.get")
    def test_malformed_and_missing_discharge_values_are_not_interpreted(self, get):
        get.return_value = self._response({
            "daily": {
                "time": ["not-a-date", "2026-10-22"],
                "river_discharge": [123.4, None],
            }
        })
        missing = get_flood_forecast(28.6, 77.2)
        self.assertFalse(missing["success"])
        self.assertIn("no usable daily", missing["error"])
        self.assertIn("checked_at_utc", missing)
        self.assertNotIn("retrieved_at_utc", missing)

        get.return_value = self._response(["unexpected"])
        malformed = get_flood_forecast(28.6, 77.2)
        self.assertFalse(malformed["success"])

    @patch("emergency_monitor.requests.get")
    def test_flood_forecast_omits_invalid_rows_and_requires_usable_data(self, get):
        get.return_value = self._response({
            "latitude": 28.625,
            "longitude": 77.225,
            "daily": {
                "time": ["2026-10-21", "2026-02-30", "2026-10-23"],
                "river_discharge": [12.5, 90.0, float("inf")],
            },
        })

        report = get_flood_forecast(28.6, 77.2)

        self.assertTrue(report["success"])
        self.assertEqual(report["daily"], [{
            "date": "2026-10-21",
            "river_discharge_m3s": 12.5,
        }])
        self.assertTrue(report["warnings"])

    def test_heavy_rain_forecast_creates_watch_indicator_with_source_time(self):
        weather = self._weather([
            {"time": "2026-10-21T12:00", "rain_mm": 18.0},
            {"time": "2026-10-21T13:00", "rain_mm": 12.5},
        ])

        assessment = assess_emergency_conditions(
            weather,
            self._flood(),
            rainfall_threshold_mm=10,
            now=self.now,
        )

        self.assertEqual(assessment["status"], "Watch")
        self.assertEqual(len(assessment["alerts"]), 1)
        alert = assessment["alerts"][0]
        self.assertEqual(alert["timing"], "2026-10-21T13:00")
        self.assertEqual(alert["rainfall_mm"], 12.5)
        self.assertEqual(alert["certainty"], "Not supplied by source; no probability estimated")

    def test_no_warning_or_below_threshold_remains_unknown_not_normal(self):
        weather = self._weather([
            {"time": "2026-10-21T13:00", "rain_mm": 2.0},
        ])

        assessment = assess_emergency_conditions(
            weather,
            self._flood(),
            now=self.now,
        )

        self.assertEqual(assessment["status"], "Unknown")
        self.assertFalse(assessment["alerts"])
        self.assertFalse(assessment["official_warning_feed_connected"])
        self.assertIn("do not mean the area is safe", assessment["status_reason"])

    def test_high_discharge_is_displayable_but_not_a_warning_without_threshold(self):
        flood = self._flood([
            {"date": "2026-10-21", "river_discharge_m3s": 999999.0},
        ])

        assessment = assess_emergency_conditions(None, flood, now=self.now)

        self.assertEqual(assessment["status"], "Unknown")
        self.assertTrue(assessment["flood_fresh"])
        self.assertFalse(assessment["alerts"])

    def test_stale_data_is_unknown_and_does_not_create_watch(self):
        old = self.now - timedelta(minutes=31)
        weather = self._weather([
            {"time": "2026-10-21T13:00", "rain_mm": 25.0},
        ], retrieved_at=old)

        assessment = assess_emergency_conditions(
            weather,
            self._flood(retrieved_at=old),
            now=self.now,
        )

        self.assertEqual(assessment["status"], "Unknown")
        self.assertFalse(assessment["weather_fresh"])
        self.assertFalse(assessment["flood_fresh"])
        self.assertFalse(assessment["alerts"])

    def test_explicitly_stale_weather_is_not_used_for_screening(self):
        weather = self._weather([
            {"time": "2026-10-21T13:00", "rain_mm": 25.0},
        ])
        weather["is_stale"] = True
        weather["cache_status"] = "stale"

        assessment = assess_emergency_conditions(
            weather,
            self._flood(),
            now=self.now,
        )

        self.assertEqual(assessment["status"], "Unknown")
        self.assertFalse(assessment["weather_fresh"])
        self.assertFalse(assessment["alerts"])

    def test_duplicate_alert_identity_is_stable_for_same_forecast_period(self):
        alert = {
            "hazard": "Heavy-rain forecast indicator",
            "timing": "2026-10-21T13:00",
        }

        identifier = emergency_alert_id(alert, 28.63177, 77.21938)

        self.assertEqual(
            identifier,
            emergency_alert_id(alert, 28.63177, 77.21938),
        )
        self.assertNotEqual(identifier, emergency_alert_id(alert, 27.7, 85.3))

    def test_forecast_indicator_updates_and_clearance_are_detected_once_by_id(self):
        previous = {
            "old-id": {
                "location": "Origin",
                "alert": {"hazard": "Heavy-rain forecast indicator"},
            },
        }
        current = {
            "new-id": {
                "location": "Origin",
                "alert": {"hazard": "Heavy-rain forecast indicator"},
            },
        }

        changes = reconcile_active_alerts(previous, current)

        self.assertEqual(changes["new"], [current["new-id"]])
        self.assertEqual(changes["cleared"], [previous["old-id"]])
        self.assertFalse(reconcile_active_alerts(current, current)["new"])
        self.assertFalse(reconcile_active_alerts(current, current)["cleared"])

    def test_last_successful_source_report_survives_a_failed_refresh(self):
        previous = {"success": True, "retrieved_at_utc": "2026-10-21T12:00:00+00:00"}
        failed = {"success": False, "error": "provider unavailable"}
        updated = {"success": True, "retrieved_at_utc": "2026-10-21T12:15:00+00:00"}

        self.assertEqual(last_successful_report(failed, previous), previous)
        self.assertEqual(last_successful_report(updated, previous), updated)
        self.assertIsNone(last_successful_report(failed, None))

    def test_invalid_screening_threshold_raises_instead_of_silently_falling_back(self):
        with self.assertRaises(ValueError):
            assess_emergency_conditions(
                self._weather([]),
                self._flood(),
                rainfall_threshold_mm=0,
                now=self.now,
            )
        self.assertEqual(DEFAULT_RAINFALL_SCREENING_THRESHOLD_MM, 10.0)


if __name__ == "__main__":
    unittest.main()
