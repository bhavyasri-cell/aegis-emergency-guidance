"""
road_network.py
Simulated Road Network for AI-Based Emergency Evacuation Route Planner.
Step 1: Graph creation, intersection definitions, and road distances.
"""

import networkx as nx


def create_road_network():
    """Creates and returns a simulated road network graph."""
    # Initialize an undirected graph to represent two-way roads
    G = nx.Graph()

    # Define intersections (nodes) with specific roles
    intersections = {
        "A": {"role": "Starting Point (Evacuation Origin)"},
        "B": {"role": "Intersection"},
        "C": {"role": "Intersection"},
        "D": {"role": "Intersection"},
        "E": {"role": "Intersection"},
        "F": {"role": "Safe Destination (Evacuation Shelter)"},
    }

    # Add intersections to the graph along with their attributes
    for node, attrs in intersections.items():
        G.add_node(node, **attrs)

    # Define roads (edges) connecting intersections with distance values (weights in km/units)
    roads = [
        ("A", "B", 4),
        ("A", "C", 2),
        ("B", "C", 1),
        ("B", "D", 5),
        ("C", "D", 8),
        ("C", "E", 10),
        ("D", "E", 2),
        ("D", "F", 6),
        ("E", "F", 3),
    ]

    # Add edges to the graph with a 'distance' attribute
    for u, v, distance in roads:
        G.add_edge(u, v, distance=distance)

    return G, "A", "F"


def main():
    # Build the network
    network, start_node, dest_node = create_road_network()

    print("=" * 50)
    print("AI EMERGENCY EVACUATION ROUTE PLANNER - STEP 1")
    print("=" * 50)

    # 1. Display start and destination points
    print(f"\n[Starting Point]     : Intersection {start_node}")
    print(f"[Safe Destination]  : Intersection {dest_node}\n")

    # 2. Display all intersections (nodes)
    print("-" * 50)
    print(f"Total Intersections: {network.number_of_nodes()}")
    print("-" * 50)
    for node, data in network.nodes(data=True):
        print(f" - Node {node}: {data.get('role', 'Intersection')}")

    # 3. Display all roads (edges) and their distances
    print("\n" + "-" * 50)
    print(f"Total Road Segments: {network.number_of_edges()}")
    print("-" * 50)
    for u, v, data in network.edges(data=True):
        print(f" - Road ({u} <--> {v}): Distance = {data['distance']} units")

    print("\n" + "=" * 50)
    print("Road network initialized successfully!")
    print("=" * 50)


if __name__ == "__main__":
    main()
