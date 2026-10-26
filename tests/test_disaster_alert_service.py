import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import requests

from disaster_alert_service import (
    alert_covers_point,
    alert_time_state,
    classify_alert_report,
    clear_alert_cache,
    fetch_location_alerts,
    report_is_stale,
    route_intersects_alert,
)


def _polygon(minimum=-1, maximum=1):
    ring = [
        [minimum, minimum],
        [maximum, minimum],
        [maximum, maximum],
        [minimum, maximum],
        [minimum, minimum],
    ]
    return {"type": "Polygon", "coordinates": [ring]}


def _alert(geometry=None, **overrides):
    alert = {
        "id": "test-alert-1",
        "hazard_type": "Flood",
        "severity": "Severe",
        "severity_level": "3",
        "affected_region": "Source area",
        "issuing_authority": "Official authority",
        "issue_time": "2026-10-20T10:00:00+05:30",
        "effective_from": "2026-10-20T10:00:00+05:30",
        "expires_at": "2026-10-22T10:00:00+05:30",
        "message": "Source-provided warning text",
        "geometry": geometry if geometry is not None else _polygon(),
    }
    alert.update(overrides)
    return alert


class DisasterAlertServiceTests(unittest.TestCase):
    def setUp(self):
        clear_alert_cache()

    @staticmethod
    def _response(payload, status_code=200):
        response = Mock()
        response.status_code = status_code
        response.json.return_value = payload
        return response

    def test_source_polygon_determines_point_applicability(self):
        alert = _alert()
        self.assertTrue(alert_covers_point(alert, 0, 0))
        self.assertFalse(alert_covers_point(alert, 5, 5))
        missing_geometry = _alert()
        missing_geometry["geometry"] = None
        self.assertIsNone(alert_covers_point(missing_geometry, 0, 0))

    def test_expired_alert_is_not_current(self):
        alert = _alert(expires_at="2020-01-01T00:00:00Z")
        now = datetime(2026, 10, 21, tzinfo=timezone.utc)
        self.assertEqual(alert_time_state(alert, now), "expired")

    def test_report_classifies_active_expired_unknown_and_empty_alerts(self):
        now = datetime(2026, 10, 21, 10, 30, tzinfo=timezone.utc)
        report = {
            "success": True,
            "retrieved_at_utc": now.isoformat(),
            "alerts": [_alert()],
        }
        self.assertEqual(classify_alert_report(report, now)["status"], "active")

        report["alerts"] = [_alert(expires_at="2020-01-01T00:00:00Z")]
        self.assertEqual(classify_alert_report(report, now)["status"], "expired")

        report["alerts"] = [_alert(effective_from=None, expires_at=None)]
        unknown = classify_alert_report(report, now)
        self.assertEqual(unknown["status"], "validity_unknown")
        self.assertEqual(unknown["validity_unknown_count"], 1)

        report["alerts"] = [_alert(expires_at="not-a-timestamp")]
        self.assertEqual(
            classify_alert_report(report, now)["status"],
            "validity_unknown",
        )

        report["alerts"] = []
        self.assertEqual(
            classify_alert_report(report, now)["status"],
            "no_matching_alerts",
        )

    def test_failed_or_stale_report_is_not_reported_as_no_alerts(self):
        now = datetime(2026, 10, 21, 10, 30, tzinfo=timezone.utc)
        self.assertEqual(
            classify_alert_report({"success": False}, now)["status"],
            "unavailable",
        )
        self.assertEqual(
            classify_alert_report({
                "success": True,
                "retrieved_at_utc": (now - timedelta(hours=1)).isoformat(),
                "alerts": [],
            }, now)["status"],
            "stale",
        )

    def test_missing_geographic_information_is_unknown(self):
        alert = _alert()
        alert["geometry"] = None
        self.assertIsNone(alert_covers_point(alert, 0, 0))
        self.assertIsNone(route_intersects_alert(alert, [[0, -2], [0, 2]]))

    def test_route_crossing_alert_polygon_with_endpoints_outside(self):
        alert = _alert()
        self.assertFalse(alert_covers_point(alert, 0, -2))
        self.assertFalse(alert_covers_point(alert, 0, 2))
        self.assertTrue(route_intersects_alert(alert, [[0, -2], [0, 2]]))

    @patch("disaster_alert_service.requests.post")
    def test_fetch_preserves_source_fields_and_deduplicates_alerts(self, post):
        source_record = {
            "identifier": "same-id",
            "disaster_type": "Flood",
            "severity": "Extreme",
            "severity_level": "4",
            "area_description": "Source-provided region",
            "alert_source": "Source authority",
            "sent": "2026-10-21T10:00:00+05:30",
            "effective_start_time": "2026-10-21T10:00:00+05:30",
            "effective_end_time": "2026-10-22T10:00:00+05:30",
            "warning_message": "Source message",
            "area_json": _polygon(),
        }
        post.return_value = self._response({
            "alerts": [source_record, dict(source_record)]
        })

        report = fetch_location_alerts(0, 0)

        self.assertTrue(report["success"])
        self.assertEqual(len(report["alerts"]), 1)
        alert = report["alerts"][0]
        self.assertEqual(alert["severity"], "Extreme")
        self.assertEqual(alert["title"], "Flood")
        self.assertEqual(alert["affected_region"], "Source-provided region")
        self.assertEqual(alert["issuing_authority"], "Source authority")
        self.assertEqual(alert["message"], "Source message")
        self.assertEqual(alert_covers_point(alert, 0, 0), True)
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["params"]["radius"], "20")

    @patch("disaster_alert_service.requests.post")
    def test_malformed_alert_records_are_counted_and_ignored(self, post):
        post.return_value = self._response({"alerts": [{}, None]})
        report = fetch_location_alerts(15, 75)

        self.assertTrue(report["success"])
        self.assertEqual(report["alerts"], [])
        self.assertEqual(report["malformed_alert_count"], 2)

    @patch("disaster_alert_service.requests.post")
    def test_successful_empty_response_is_cached(self, post):
        post.return_value = self._response({"alerts": []})

        first = fetch_location_alerts(12.5, 76.5)
        second = fetch_location_alerts(12.5, 76.5)

        self.assertTrue(first["success"])
        self.assertEqual(second["alerts"], [])
        post.assert_called_once()

    @patch("disaster_alert_service.requests.post")
    def test_failed_request_returns_error_and_is_not_cached(self, post):
        post.side_effect = requests.exceptions.Timeout()

        first = fetch_location_alerts(12.5, 76.5)
        second = fetch_location_alerts(12.5, 76.5)

        self.assertFalse(first["success"])
        self.assertIn("timed out", first["error"])
        self.assertFalse(second["success"])
        self.assertEqual(post.call_count, 2)

    @patch("disaster_alert_service.requests.post")
    def test_http_failure_and_malformed_payload_are_reported(self, post):
        post.return_value = self._response({}, status_code=503)
        http_failure = fetch_location_alerts(11, 77)
        self.assertFalse(http_failure["success"])
        self.assertIn("HTTP 503", http_failure["error"])

        post.return_value = self._response({"alerts": "not a list"})
        malformed = fetch_location_alerts(11, 78)
        self.assertFalse(malformed["success"])
        self.assertIn("unexpected response format", malformed["error"])

    def test_old_successful_report_is_stale(self):
        old_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.assertTrue(report_is_stale({
            "success": True,
            "retrieved_at_utc": old_time,
        }))
        self.assertTrue(report_is_stale({"success": False}))


if __name__ == "__main__":
    unittest.main()
