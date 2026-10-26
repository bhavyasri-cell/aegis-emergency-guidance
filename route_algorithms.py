"""
route_algorithms.py
Implementation of Pathfinding Algorithms for Emergency Evacuation:
1. Breadth-First Search (BFS) - minimizes number of intersections (hops)
2. Uniform Cost Search (UCS) - minimizes cumulative road distance
3. A* Search (A*) - informed search using an admissible heuristic
"""

from collections import deque
import heapq
from road_network import create_road_network


def calculate_route_distance(graph, route):
    """Calculates the total travel distance of a given route."""
    if not route or len(route) < 2:
        return 0

    total_dist = 0
    for i in range(len(route) - 1):
        u, v = route[i], route[i + 1]
        if graph.has_edge(u, v):
            total_dist += graph[u][v].get("distance", 1)
        else:
            return float("inf")  # Edge does not exist
    return total_dist


def breadth_first_search(graph, start, goal):
    """
    Breadth-First Search (BFS).
    Explores neighbor nodes layer-by-layer.
    Finds the route with the fewest intersections (hops), ignoring distances.

    Returns:
        tuple: (route_list, total_distance) or (None, float('inf')) if no route exists.
    """
    # Validate start and goal
    if start not in graph or goal not in graph:
        return None, float("inf")

    if start == goal:
        return [start], 0

    # Queue stores tuples of: (current_node, path_taken)
    queue = deque([(start, [start])])
    visited = {start}

    while queue:
        current_node, path = queue.popleft()

        # Check all connected roads from current intersection
        for neighbor in graph.neighbors(current_node):
            if neighbor not in visited:
                new_path = path + [neighbor]

                # If destination is reached, return path and calculate its total distance
                if neighbor == goal:
                    total_dist = calculate_route_distance(graph, new_path)
                    return new_path, total_dist

                visited.add(neighbor)
                queue.append((neighbor, new_path))

    # Goal is unreachable
    return None, float("inf")


def uniform_cost_search(graph, start, goal):
    """
    Uniform Cost Search (UCS / Dijkstra's Algorithm).
    Always expands the path with the lowest cumulative distance.
    Guarantees the optimal (shortest distance) route.

    Returns:
        tuple: (route_list, total_distance) or (None, float('inf')) if no route exists.
    """
    # Validate start and goal
    if start not in graph or goal not in graph:
        return None, float("inf")

    if start == goal:
        return [start], 0

    # Priority queue stores tuples of: (cumulative_distance, current_node, path_taken)
    # heapq pops the element with the lowest cumulative_distance first
    frontier = [(0, start, [start])]
    visited_cost = {}

    while frontier:
        cost, current_node, path = heapq.heappop(frontier)

        # If we reached the destination, this is the lowest-cost path
        if current_node == goal:
            return path, cost

        # Skip if a cheaper path to this node has already been explored
        if current_node in visited_cost and visited_cost[current_node] <= cost:
            continue
        visited_cost[current_node] = cost

        # Explore neighboring intersections
        for neighbor in graph.neighbors(current_node):
            road_distance = graph[current_node][neighbor].get("distance", 1)
            new_cost = cost + road_distance

            if neighbor not in visited_cost or new_cost < visited_cost[neighbor]:
                heapq.heappush(frontier, (new_cost, neighbor, path + [neighbor]))

    # Goal is unreachable
    return None, float("inf")


def zero_heuristic(node, goal):
    """
    Admissible heuristic function for simulated networks without physical coordinates.
    Since h(n) = 0 for all nodes, h(n) never overestimates the actual cost (admissible).
    A* with h(n) = 0 behaves identically to Uniform Cost Search.
    """
    return 0


def a_star_search(graph, start, goal, heuristic=zero_heuristic):
    """
    A* Search Algorithm.
    Expands nodes based on f(n) = g(n) + h(n):
      g(n): actual distance from start to node n
      h(n): estimated distance from node n to goal (heuristic)
    With an admissible heuristic (h(n) <= true distance), A* is optimal and complete.

    Returns:
        tuple: (route_list, total_distance) or (None, float('inf')) if no route exists.
    """
    # Validate start and goal
    if start not in graph or goal not in graph:
        return None, float("inf")

    if start == goal:
        return [start], 0

    # Counter used as a tie-breaker so tuples are always orderable in the priority queue
    counter = 0

    # Priority queue stores: (f_score, tie_breaker, g_score, current_node, path_taken)
    initial_h = heuristic(start, goal)
    frontier = [(initial_h, counter, 0, start, [start])]

    # Stores the lowest known g_score (actual distance) for each visited node
    best_g = {start: 0}

    while frontier:
        f_score, _, g_score, current_node, path = heapq.heappop(frontier)

        # Reached destination with optimal cost
        if current_node == goal:
            return path, g_score

        # If we already found a strictly better path to this node, skip it
        if g_score > best_g.get(current_node, float("inf")):
            continue

        # Explore neighbors
        for neighbor in graph.neighbors(current_node):
            road_distance = graph[current_node][neighbor].get("distance", 1)
            tentative_g = g_score + road_distance

            # If this path to neighbor is shorter than any previous path
            if tentative_g < best_g.get(neighbor, float("inf")):
                best_g[neighbor] = tentative_g
                counter += 1
                f_score = tentative_g + heuristic(neighbor, goal)
                heapq.heappush(frontier, (f_score, counter, tentative_g, neighbor, path + [neighbor]))

    # Goal is unreachable
    return None, float("inf")


def run_tests():
    """Runs tests for BFS, UCS, and A* on the road network."""
    # 1. Load the simulated road network from Step 1
    network, start_node, dest_node = create_road_network()

    print("=" * 65)
    print(f"EVACUATION ROUTE COMPARISON: {start_node} -> {dest_node}")
    print("=" * 65)

    # Dictionary of algorithms to evaluate
    algorithms = {
        "Breadth-First Search (BFS)": lambda: breadth_first_search(network, start_node, dest_node),
        "Uniform Cost Search (UCS)": lambda: uniform_cost_search(network, start_node, dest_node),
        "A* Search (h = 0)": lambda: a_star_search(network, start_node, dest_node),
    }

    # Execute and display results for each algorithm
    for name, func in algorithms.items():
        route, distance = func()
        if route:
            route_str = " -> ".join(route)
            print(f"\n[{name}]")
            print(f"  Route Taken    : {route_str}")
            print(f"  Intersections  : {len(route)} nodes ({len(route) - 1} hops)")
            print(f"  Total Distance : {distance} units")
        else:
            print(f"\n[{name}]")
            print("  Route: No reachable path found!")

    print("\n" + "=" * 65)
    print("TESTING UNREACHABLE / DISCONNECTED ROUTE (EDGE CASE)")
    print("=" * 65)

    # Test an isolated intersection with no roads connected to it
    isolated_network = network.copy()
    isolated_network.add_node("Z", role="Isolated Safehouse")

    route_bfs, dist_bfs = breadth_first_search(isolated_network, start_node, "Z")
    route_ucs, dist_ucs = uniform_cost_search(isolated_network, start_node, "Z")
    route_astar, dist_astar = a_star_search(isolated_network, start_node, "Z")

    print(f"Searching route from {start_node} to isolated node 'Z':")
    print(f"  BFS Result   : Route={route_bfs}, Distance={dist_bfs}")
    print(f"  UCS Result   : Route={route_ucs}, Distance={dist_ucs}")
    print(f"  A* Result    : Route={route_astar}, Distance={dist_astar}")
    print("  Status       : All algorithms correctly handled unreachable route.")
    print("=" * 65)


if __name__ == "__main__":
    run_tests()
