import unittest
from unittest.mock import patch

import requests

import multi_route_planner
import real_routing


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self.body = [] if body is None else body

    def json(self):
        return self.body


class GeocodeLocationTests(unittest.TestCase):
    def setUp(self):
        with real_routing._GEOCODE_CACHE_LOCK:
            real_routing._GEOCODE_CACHE.clear()

    def test_valid_destination_returns_coordinates_and_is_cached(self):
        response = FakeResponse(
            body=[{
                "lat": "28.6129",
                "lon": "77.2295",
                "display_name": "India Gate, New Delhi",
            }]
        )
        with patch.object(real_routing, "_nominatim_get", return_value=response) as get:
            first = real_routing.geocode_location("India Gate, New Delhi")
            second = real_routing.geocode_location("  INDIA GATE, NEW DELHI  ")

        self.assertEqual(first, (28.6129, 77.2295, "India Gate, New Delhi"))
        self.assertEqual(second, first)
        get.assert_called_once()

    def test_geocoded_destination_coordinates_feed_real_route_generation(self):
        response = FakeResponse(
            body=[{
                "lat": "28.6129",
                "lon": "77.2295",
                "display_name": "India Gate, New Delhi",
            }]
        )
        primary_route = {
            "success": True,
            "distance_km": 1.0,
            "duration_mins": 5.0,
            "route_coords": [(28.61, 77.22), (28.6129, 77.2295)],
            "steps": [],
        }
        with (
            patch.object(real_routing, "_nominatim_get", return_value=response),
            patch.object(
                multi_route_planner,
                "get_real_road_route",
                return_value=primary_route,
            ) as route_request,
            patch.object(multi_route_planner, "query_osrm_via", return_value=None),
        ):
            latitude, longitude, _ = real_routing.geocode_location(
                "India Gate, New Delhi"
            )
            routes = multi_route_planner.generate_real_world_multi_routes(
                28.61,
                77.22,
                latitude,
                longitude,
            )

        self.assertIn("primary", routes)
        self.assertEqual(route_request.call_args.args, (28.61, 77.22, 28.6129, 77.2295))

    def test_failed_lookup_is_not_cached_and_can_be_retried(self):
        with patch.object(
            real_routing,
            "_nominatim_get",
            side_effect=[
                requests.exceptions.Timeout("read timed out"),
                requests.exceptions.Timeout("read timed out"),
                FakeResponse(body=[{"lat": "10", "lon": "20", "display_name": "Place"}]),
            ],
        ) as get, patch.object(real_routing.time, "sleep"):
            failure = real_routing.geocode_location("Retry Place")
            success = real_routing.geocode_location("Retry Place")

        self.assertIsNone(failure[0])
        self.assertEqual(success, (10.0, 20.0, "Place"))
        self.assertEqual(get.call_count, 3)

    def test_unknown_destination_returns_empty_result_message(self):
        with patch.object(
            real_routing, "_nominatim_get", return_value=FakeResponse(body=[])
        ) as get:
            result = real_routing.geocode_location("Unknown Place, Nowhere")

        self.assertIsNone(result[0])
        self.assertIsNone(result[1])
        self.assertIn("could not be found", result[2])
        get.assert_called_once()

    def test_timeout_retries_once_then_returns_clear_error(self):
        timeout = requests.exceptions.Timeout("read timed out")
        with (
            patch.object(
                real_routing,
                "_nominatim_get",
                side_effect=[timeout, timeout],
            ) as get,
            patch.object(real_routing.time, "sleep"),
        ):
            result = real_routing.geocode_location("Timeout Place")

        self.assertIsNone(result[0])
        self.assertIn("timed out after one retry", result[2])
        self.assertEqual(get.call_count, 2)

    def test_connection_failure_retries_once_then_returns_clear_error(self):
        error = requests.exceptions.ConnectionError("offline")
        with (
            patch.object(
                real_routing,
                "_nominatim_get",
                side_effect=[error, error],
            ) as get,
            patch.object(real_routing.time, "sleep"),
        ):
            result = real_routing.geocode_location("Network Failure Place")

        self.assertIsNone(result[0])
        self.assertIn("Network error during geocoding", result[2])
        self.assertEqual(get.call_count, 2)

    def test_server_error_retries_but_client_error_does_not(self):
        with (
            patch.object(
                real_routing,
                "_nominatim_get",
                side_effect=[
                    FakeResponse(status_code=503),
                    FakeResponse(body=[{"lat": "1", "lon": "2", "display_name": "Place"}]),
                ],
            ) as get,
            patch.object(real_routing.time, "sleep"),
        ):
            success = real_routing.geocode_location("Temporary Server Error Place")
        self.assertEqual(success[:2], (1.0, 2.0))
        self.assertEqual(get.call_count, 2)

        with patch.object(
            real_routing, "_nominatim_get", return_value=FakeResponse(status_code=404)
        ) as get:
            failure = real_routing.geocode_location("HTTP Error Place")
        self.assertIn("HTTP error 404", failure[2])
        get.assert_called_once()

    def test_invalid_destination_does_not_make_request(self):
        with patch.object(real_routing, "_nominatim_get") as get:
            result = real_routing.geocode_location("   ")
        self.assertIsNone(result[0])
        self.assertIn("valid location", result[2])
        get.assert_not_called()

    def test_timeout_is_configurable_with_environment_setting(self):
        with (
            patch.dict("os.environ", {"NOMINATIM_TIMEOUT_SECONDS": "4.5"}),
            patch.object(
                real_routing,
                "_nominatim_get",
                return_value=FakeResponse(body=[{"lat": "10", "lon": "20"}]),
            ) as get,
        ):
            real_routing.geocode_location("Configured Timeout Place")

        self.assertEqual(get.call_args.args[2], 4.5)


if __name__ == "__main__":
    unittest.main()
