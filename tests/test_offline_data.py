import json
import os
import tempfile
import unittest
from unittest.mock import patch

from offline_data import (
    database_summary,
    generate_local_routes,
    import_geojson,
    load_cached_report,
    render_local_map_svg,
    save_cached_report,
    search_local_places,
)


class OfflineDataTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = os.path.join(
            self.temporary_directory.name,
            "offline.sqlite3",
        )
        self.environment = patch.dict(
            os.environ,
            {"AEGIS_OFFLINE_DB": self.database_path},
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary_directory.cleanup()

    @staticmethod
    def _sample_geojson():
        return json.dumps({
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"id": "road-1", "name": "Local Main Road"},
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [
                            [77.0, 28.0],
                            [77.01, 28.0],
                            [77.02, 28.0],
                        ],
                    },
                },
                {
                    "type": "Feature",
                    "properties": {"name": "Origin Place"},
                    "geometry": {"type": "Point", "coordinates": [77.0, 28.0]},
                },
                {
                    "type": "Feature",
                    "properties": {"name": "Community Shelter", "category": "shelter"},
                    "geometry": {"type": "Point", "coordinates": [77.02, 28.0]},
                },
            ],
        })

    def test_imported_places_and_shelters_search_without_network(self):
        result = import_geojson(self._sample_geojson(), "test-area.geojson")

        self.assertEqual(result["road_segments"], 2)
        self.assertEqual(result["places"], 2)
        self.assertEqual(result["shelters"], 1)
        origin = search_local_places("Origin Place")
        shelter = search_local_places("Community", category="shelter")
        self.assertTrue(origin["success"])
        self.assertTrue(shelter["success"])
        self.assertEqual(shelter["place"]["category"], "shelter")
        self.assertEqual(database_summary()["road_segments"], 2)

    def test_offline_route_uses_imported_local_roads_and_does_not_guess_time(self):
        import_geojson(self._sample_geojson())

        routes = generate_local_routes(28.0, 77.0, 28.0, 77.02)

        self.assertIn("primary", routes)
        self.assertAlmostEqual(routes["primary"]["distance_km"], 1.96, places=2)
        self.assertIsNone(routes["primary"]["duration_mins"])
        self.assertIn("local road graph", routes["primary"]["rationale"])
        self.assertEqual(routes["primary"]["source"], "Imported local GeoJSON road data")

    def test_empty_coverage_fails_explicitly(self):
        result = generate_local_routes(28.0, 77.0, 28.0, 77.02)

        self.assertIn("No offline road dataset", result["error"])

    def test_places_outside_coverage_and_disconnected_roads_are_not_fabricated(self):
        import_geojson(self._sample_geojson())
        outside = generate_local_routes(40.0, 77.0, 28.0, 77.02)
        self.assertIn("outside the installed road coverage", outside["error"])

        disconnected = import_geojson(json.dumps({
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "properties": {"name": "Isolated Road"},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[78.0, 28.0], [78.01, 28.0]],
                },
            }],
        }))
        self.assertEqual(disconnected["road_segments"], 1)
        no_path = generate_local_routes(28.0, 77.0, 28.0, 78.01)
        self.assertIn("no connected route", no_path["error"])

    def test_invalid_geojson_coordinates_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "out-of-range"):
            import_geojson(json.dumps({
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "properties": {"name": "Invalid road"},
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[181, 28], [77, 28]],
                    },
                }],
            }))

    def test_local_map_is_self_contained_svg(self):
        svg = render_local_map_svg(
            [(28.0, 77.0, 28.0, 77.01)],
            {"primary": {
                "color": "#2f855a",
                "route_coords": [[28.0, 77.0], [28.0, 77.01]],
            }},
            [28.0, 77.0],
            [28.0, 77.01],
            "Origin",
            "Destination",
        )
        self.assertIn("<svg", svg)
        self.assertIn("Imported data coverage only", svg)
        self.assertNotIn("https://", svg)

    def test_successful_reports_can_be_loaded_offline(self):
        report = {
            "success": True,
            "retrieved_at_utc": "2026-10-24T00:00:00+00:00",
            "current": {"temperature_c": 20},
        }
        save_cached_report("weather", 28.0, 77.0, report)

        self.assertEqual(load_cached_report("weather", 28.0, 77.0), report)

    def test_failed_reports_are_not_saved_as_cached_successes(self):
        save_cached_report("weather", 28.0, 77.0, {"success": False})

        self.assertIsNone(load_cached_report("weather", 28.0, 77.0))


if __name__ == "__main__":
    unittest.main()
