import math
import unittest

from evacuation_simulator import simulate_rerouting
from road_network import create_road_network
from route_algorithms import (
    a_star_search,
    breadth_first_search,
    calculate_route_distance,
    uniform_cost_search,
)


class RouteAlgorithmTests(unittest.TestCase):
    def setUp(self):
        self.graph, self.start, self.destination = create_road_network()

    def test_breadth_first_search_returns_fewest_hops(self):
        route, distance = breadth_first_search(
            self.graph, self.start, self.destination
        )

        self.assertEqual(route, ["A", "B", "D", "F"])
        self.assertEqual(distance, 15)

    def test_uniform_cost_search_returns_shortest_distance(self):
        route, distance = uniform_cost_search(
            self.graph, self.start, self.destination
        )

        self.assertEqual(route, ["A", "C", "B", "D", "E", "F"])
        self.assertEqual(distance, 13)
        self.assertEqual(calculate_route_distance(self.graph, route), distance)

    def test_zero_heuristic_a_star_matches_uniform_cost_search(self):
        ucs_route, ucs_distance = uniform_cost_search(
            self.graph, self.start, self.destination
        )
        astar_route, astar_distance = a_star_search(
            self.graph, self.start, self.destination
        )

        self.assertEqual(astar_distance, ucs_distance)
        self.assertEqual(astar_route, ucs_route)
        self.assertEqual(astar_distance, 13)

    def test_blocked_road_reroutes_without_mutating_base_network(self):
        result = simulate_rerouting(
            self.graph,
            self.start,
            self.destination,
            blocked_roads=[("B", "D")],
        )

        self.assertEqual(result["updated_route"], ["A", "C", "D", "E", "F"])
        self.assertEqual(result["updated_distance"], 15)
        self.assertTrue(self.graph.has_edge("B", "D"))

    def test_unreachable_and_unknown_destinations_are_reported(self):
        disconnected = self.graph.copy()
        disconnected.remove_edges_from(list(disconnected.edges("F")))
        disconnected.add_node("Z")

        for search in (
            breadth_first_search,
            uniform_cost_search,
            a_star_search,
        ):
            with self.subTest(search=search.__name__):
                self.assertEqual(
                    search(disconnected, self.start, "F"),
                    (None, math.inf),
                )
                self.assertEqual(
                    search(self.graph, self.start, "unknown"),
                    (None, math.inf),
                )

    def test_start_equal_to_destination_has_zero_distance(self):
        for search in (
            breadth_first_search,
            uniform_cost_search,
            a_star_search,
        ):
            with self.subTest(search=search.__name__):
                self.assertEqual(search(self.graph, "A", "A"), (["A"], 0))

    def test_route_distance_reports_missing_edges_as_unreachable(self):
        self.assertEqual(calculate_route_distance(self.graph, ["A", "F"]), math.inf)


class DeploymentAssetTests(unittest.TestCase):
    def test_geolocation_component_is_present_beside_the_application(self):
        from pathlib import Path

        project_root = Path(__file__).resolve().parents[1]
        component_file = project_root / "location_component" / "index.html"

        self.assertTrue(component_file.is_file())
        component_html = component_file.read_text(encoding="utf-8")
        self.assertIn('id="request-location"', component_html)
        self.assertIn("button.addEventListener(\"click\"", component_html)
        self.assertIn("navigator.geolocation.getCurrentPosition", component_html)


if __name__ == "__main__":
    unittest.main()
