"""
evacuation_simulator.py
Step 3: Blocked-Road Handling and Automatic Rerouting for Emergency Evacuation.

Reuses:
- road_network.py -> create_road_network()
- route_algorithms.py -> uniform_cost_search()
"""

from road_network import create_road_network
from route_algorithms import uniform_cost_search


def simulate_rerouting(base_graph, start="A", destination="F", blocked_roads=None):
    """
    Simulates emergency evacuation with dynamic road closures and automatic rerouting.

    Args:
        base_graph (networkx.Graph): The baseline road network.
        start (str): Origin intersection (default: 'A').
        destination (str): Evacuation destination (default: 'F').
        blocked_roads (list of tuples): Road segments to block, e.g., [('B', 'D')].

    Returns:
        dict: Summary of simulation results before and after road closures.
    """
    if blocked_roads is None:
        blocked_roads = []

    # 1. Create a fresh working copy of the graph to ensure test independence
    working_graph = base_graph.copy()

    # 2. Find the baseline optimal route before any hazard or road blockages
    original_route, original_distance = uniform_cost_search(working_graph, start, destination)

    # 3. Apply road closures (bidirectional edge removal)
    actually_blocked = []
    for road in blocked_roads:
        u, v = road[0], road[1]
        # In an undirected NetworkX graph, removing edge (u, v) removes both directions
        if working_graph.has_edge(u, v):
            working_graph.remove_edge(u, v)
            actually_blocked.append((u, v))
        else:
            print(f"  [Warning] Road {u} <--> {v} does not exist or was already blocked.")

    # 4. Recalculate route after road closures using Uniform Cost Search (UCS)
    updated_route, updated_distance = uniform_cost_search(working_graph, start, destination)

    # 5. Package results
    return {
        "start": start,
        "destination": destination,
        "original_route": original_route,
        "original_distance": original_distance,
        "blocked_roads": actually_blocked,
        "updated_route": updated_route,
        "updated_distance": updated_distance,
        "is_reachable": updated_route is not None,
    }


def display_simulation_report(title, result):
    """Prints a clear, formatted comparison of the evacuation routes."""
    print("\n" + "=" * 65)
    print(f"SCENARIO: {title}")
    print("=" * 65)
    print(f"Origin Intersection : {result['start']}")
    print(f"Safe Destination    : {result['destination']}")

    # Display original baseline route
    orig_str = " -> ".join(result["original_route"]) if result["original_route"] else "None"
    print(f"\n[Original Baseline Route (Before Blockage)]")
    print(f"  Path     : {orig_str}")
    print(f"  Distance : {result['original_distance']} units")

    # Display blocked roads
    print(f"\n[Emergency Hazards / Blocked Roads]")
    if result["blocked_roads"]:
        for u, v in result["blocked_roads"]:
            print(f"  - Road {u} <--> {v} [CLOSED IN BOTH DIRECTIONS]")
    else:
        print("  - None (All roads open)")

    # Display updated route or unreachable alert
    print(f"\n[Updated Evacuation Route (After Blockage)]")
    if result["is_reachable"]:
        upd_str = " -> ".join(result["updated_route"])
        detour = result["updated_distance"] - result["original_distance"]
        print(f"  Path     : {upd_str}")
        print(f"  Distance : {result['updated_distance']} units")
        if detour > 0:
            print(f"  Detour   : +{detour} units longer than the original optimal route")
        else:
            print("  Detour   : 0 units (Route unaffected)")
    else:
        print("  Status   : DESTINATION UNREACHABLE!")
        print("  Alert    : All evacuation paths to the shelter are cut off by closures.")
    print("=" * 65)


def run_all_simulations():
    """Runs test scenarios demonstrating single, multiple, and critical road blocks."""
    # Build baseline road network from Step 1
    base_network, start_node, dest_node = create_road_network()

    # Scenario 1: Single Blocked Road (B-D)
    # Original route went through B-D (A -> C -> B -> D -> E -> F)
    result_1 = simulate_rerouting(
        base_graph=base_network,
        start=start_node,
        destination=dest_node,
        blocked_roads=[("B", "D")],
    )
    display_simulation_report("1. Single Blocked Road (Hazard on B <--> D)", result_1)

    # Scenario 2: Multiple Blocked Roads (B-D and C-E)
    # Further constrains available evacuation corridors
    result_2 = simulate_rerouting(
        base_graph=base_network,
        start=start_node,
        destination=dest_node,
        blocked_roads=[("B", "D"), ("C", "E")],
    )
    display_simulation_report("2. Multiple Blocked Roads (B <--> D and C <--> E)", result_2)

    # Scenario 3: Complete Isolation / Unreachable Destination
    # Destination F connects only to D and E. Blocking both cuts off all access to F.
    result_3 = simulate_rerouting(
        base_graph=base_network,
        start=start_node,
        destination=dest_node,
        blocked_roads=[("D", "F"), ("E", "F")],
    )
    display_simulation_report("3. Critical Road Closures (Destination Unreachable)", result_3)


if __name__ == "__main__":
    run_all_simulations()
