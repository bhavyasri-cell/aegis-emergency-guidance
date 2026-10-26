import math
import unittest
from unittest.mock import patch

import app as aegis_app
import folium
import real_routing
from multi_route_planner import (
    are_routes_distinct,
    check_route_hazard_proximity,
    count_distinct_destination_routes,
    generate_real_world_multi_routes,
    resolve_route_selection,
)


def _provider_route(coords, distance_km, duration_mins):
    return {
        "success": True,
        "route_coords": coords,
        "distance_km": distance_km,
        "duration_mins": duration_mins,
        "steps": [{
            "instruction": f"Route with distance {distance_km} km",
            "distance_m": distance_km * 1000,
        }],
    }


class RouteHazardProximityTests(unittest.TestCase):
    def test_segment_crossing_hazard_is_detected_when_endpoints_are_outside(self):
        intersects, min_distance = check_route_hazard_proximity(
            [[0.0, -1.0], [0.0, 1.0]],
            hazard_lat=0.0,
            hazard_lon=0.0,
            radius_km=0.05,
        )

        self.assertTrue(intersects)
        self.assertAlmostEqual(min_distance, 0.0, places=6)

    def test_minimum_distance_is_measured_along_closest_route_segment(self):
        intersects, min_distance = check_route_hazard_proximity(
            [[0.0, -2.0], [0.0, 0.0], [0.0, 2.0]],
            hazard_lat=0.1,
            hazard_lon=1.0,
            radius_km=1.0,
        )

        expected_distance = 6371.0 * math.radians(0.1)
        self.assertFalse(intersects)
        self.assertAlmostEqual(min_distance, expected_distance, places=3)


class RealWorldRouteGenerationTests(unittest.TestCase):
    def setUp(self):
        self.primary = _provider_route(
            [[0.0, 0.0], [0.0, 0.5], [0.0, 1.0], [0.0, 2.0]],
            12.0,
            18.0,
        )
        self.shorter_alternative = _provider_route(
            [[0.0, 0.0], [0.005, 0.5], [0.0, 1.0], [0.0, 2.0]],
            10.0,
            20.0,
        )
        self.duplicate_alternative = _provider_route(
            [
                [0.0, 0.0],
                [0.0, 0.25],
                [0.0, 0.5],
                [0.0, 1.0],
                [0.0, 2.0],
            ],
            12.1,
            19.0,
        )
        self.third_route = _provider_route(
            [[0.0, 0.0], [-0.005, 0.5], [0.0, 1.0], [0.0, 2.0]],
            14.0,
            22.0,
        )
        self.fourth_route = _provider_route(
            [[0.0, 0.0], [0.01, 0.5], [0.0, 1.0], [0.0, 2.0]],
            16.0,
            24.0,
        )

    def test_distinct_detection_does_not_depend_on_the_midpoint(self):
        first = [[0.0, 0.0], [0.0, 1.0], [0.0, 2.0]]
        second = [[0.0, 0.0], [0.005, 0.5], [0.0, 1.0], [0.0, 2.0]]

        self.assertTrue(are_routes_distinct(first, second))

    def test_generation_filters_duplicates_and_orders_shortest_distance_first(self):
        with patch(
            "multi_route_planner.get_real_road_route",
            return_value={
                **self.primary,
                "alternatives": [
                    self.duplicate_alternative,
                    self.shorter_alternative,
                    self.third_route,
                ],
            },
        ):
            routes = generate_real_world_multi_routes(0, 0, 0, 2)

        self.assertEqual(list(routes), ["primary", "alt_1", "alt_2"])
        self.assertEqual(routes["primary"]["distance_km"], 10.0)
        self.assertEqual(routes["alt_1"]["distance_km"], 12.0)
        self.assertEqual(routes["alt_2"]["distance_km"], 14.0)
        self.assertEqual(count_distinct_destination_routes(routes), 3)

    def test_generation_keeps_three_provider_alternatives(self):
        with patch(
            "multi_route_planner.get_real_road_route",
            return_value={
                **self.primary,
                "alternatives": [
                    self.shorter_alternative,
                    self.third_route,
                    self.fourth_route,
                ],
            },
        ):
            routes = generate_real_world_multi_routes(0, 0, 0, 2)

        self.assertEqual(
            list(routes),
            ["primary", "alt_1", "alt_2", "alt_3"],
        )
        self.assertEqual(routes["alt_3"]["distance_km"], 16.0)
        self.assertEqual(count_distinct_destination_routes(routes), 4)

    def test_single_provider_route_is_not_fabricated_into_alternatives(self):
        with patch(
            "multi_route_planner.get_real_road_route",
            return_value={**self.primary, "alternatives": []},
        ):
            routes = generate_real_world_multi_routes(0, 0, 0, 2)

        self.assertEqual(list(routes), ["primary"])
        self.assertEqual(count_distinct_destination_routes(routes), 1)

    def test_clear_hazard_alias_does_not_count_as_another_route(self):
        with patch(
            "multi_route_planner.get_real_road_route",
            return_value={**self.primary, "alternatives": []},
        ):
            routes = generate_real_world_multi_routes(
                0,
                0,
                0,
                2,
                hazard_info={
                    "lat": 10.0,
                    "lon": 10.0,
                    "radius_km": 0.2,
                    "description": "test hazard",
                },
            )

        self.assertEqual(routes["hazard_avoid"]["alias_of"], "primary")
        self.assertEqual(count_distinct_destination_routes(routes), 1)

    def test_route_selection_resolves_selected_route_and_steps(self):
        routes = {
            "primary": self.primary,
            "alt_1": self.shorter_alternative,
        }

        selected_id, selected_route = resolve_route_selection(routes, "alt_1")

        self.assertEqual(selected_id, "alt_1")
        self.assertEqual(selected_route["distance_km"], 10.0)
        self.assertEqual(
            selected_route["steps"][0]["instruction"],
            "Route with distance 10.0 km",
        )

    def test_map_renders_each_distinct_corridor_once_and_emphasizes_selection(self):
        routes = {
            "primary": {
                **self.primary,
                "id": "primary",
                "name": "Primary route",
                "color": "#2f855a",
                "hazard_status": "Not evaluated",
            },
            "alt_1": {
                **self.shorter_alternative,
                "id": "alt_1",
                "name": "Alternative route",
                "color": "#3182ce",
                "hazard_status": "Not evaluated",
            },
            "hazard_avoid": {
                **self.shorter_alternative,
                "id": "hazard_avoid",
                "name": "Hazard avoidance alias",
                "color": "#3182ce",
                "alias_of": "alt_1",
            },
        }

        route_map = aegis_app.build_multi_route_map(
            0,
            0,
            "Start",
            0,
            2,
            "Destination",
            routes,
            highlight_route_id="alt_1",
        )
        colored_lines = [
            child
            for child in route_map._children.values()
            if isinstance(child, folium.PolyLine)
            and child.options.get("color") in {"#2f855a", "#3182ce"}
        ]
        colored_by_color = {
            child.options["color"]: child for child in colored_lines
        }

        self.assertEqual(set(colored_by_color), {"#2f855a", "#3182ce"})
        self.assertEqual(len(colored_lines), 2)
        self.assertEqual(colored_by_color["#3182ce"].options["weight"], 9)
        self.assertEqual(colored_by_color["#2f855a"].options["weight"], 7)

    def test_legend_only_lists_routes_with_drawn_geometry(self):
        routes = {
            "primary": {
                **self.primary,
                "name": "Shortest route",
                "color": "#2f855a",
            },
            "hazard_avoid": {
                **self.primary,
                "name": "Duplicate alias",
                "color": "#2f855a",
                "alias_of": "primary",
            },
        }
        rendered = []
        with patch.object(
            aegis_app.st,
            "markdown",
            side_effect=lambda content, **kwargs: rendered.append(content),
        ):
            aegis_app.render_multi_route_legend(routes)

        self.assertIn("Shortest route", rendered[0])
        self.assertNotIn("Duplicate alias", rendered[0])
        self.assertNotIn("Alternative Route 1", rendered[0])

    def test_component_path_is_resolved_from_the_application_directory(self):
        from pathlib import Path

        expected = Path(aegis_app.__file__).resolve().parent / "location_component"

        self.assertEqual(aegis_app._LOCATION_COMPONENT_PATH, expected)
        self.assertTrue((expected / "index.html").is_file())

    def test_osrm_request_enables_alternatives_and_parses_provider_routes(self):
        route_payloads = []
        for index in range(4):
            latitude = index * 0.01
            route_payloads.append({
                "distance": 1200 + index * 100,
                "duration": 300 + index * 10,
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [0, 0],
                        [0.5, latitude],
                        [1, 1],
                    ],
                },
                "legs": [],
            })
        response = type("Response", (), {
            "status_code": 200,
            "json": lambda self: {
                "code": "Ok",
                "routes": route_payloads,
            },
        })()
        with patch.object(real_routing.requests, "get", return_value=response) as get:
            result = real_routing.get_real_road_route(0, 0, 1, 1)

        self.assertTrue(result["success"])
        self.assertEqual(len(result["alternatives"]), 3)
        self.assertEqual(get.call_args.kwargs["params"]["alternatives"], 3)


if __name__ == "__main__":
    unittest.main()
