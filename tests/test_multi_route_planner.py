import math
import unittest

from multi_route_planner import check_route_hazard_proximity


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


if __name__ == "__main__":
    unittest.main()
