import logging
import math

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

logger = logging.getLogger(__name__)


def _time_limit_for(n_stops):
    if n_stops <= 10:
        return 1
    if n_stops <= 50:
        return 3
    return 5


def haversine_distance(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return float("inf")
    try:
        lat1, lon1, lat2, lon2 = map(float, [lat1, lon1, lat2, lon2])
    except (TypeError, ValueError):
        return float("inf")

    radius_km = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return radius_km * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def solve_route_order(hub_lat, hub_lon, rows, distance_matrix_m=None):
    """Order `rows` as an open path starting at (hub_lat, hub_lon).

    distance_matrix_m: optional square table in metres over [hub] + rows
    (index 0 = hub, i = rows[i-1]), e.g. road distances from
    app/services/road_matrix.py. Without it, straight-line distance is used,
    exactly as before.
    """
    n = len(rows)
    if n == 0:
        return []
    if n == 1:
        return list(rows)

    dummy_end_node = n + 1
    num_nodes = n + 2

    def node_latlon(node):
        if node == 0:
            return hub_lat, hub_lon
        return rows[node - 1].latitude, rows[node - 1].longitude

    distance_matrix = [[0] * num_nodes for _ in range(num_nodes)]
    for i in range(num_nodes):
        if i == dummy_end_node:
            continue
        lat_i, lon_i = node_latlon(i)
        for j in range(num_nodes):
            if i == j or j == dummy_end_node:
                continue
            if distance_matrix_m is not None:
                distance_matrix[i][j] = int(distance_matrix_m[i][j])
                continue
            lat_j, lon_j = node_latlon(j)
            distance_matrix[i][j] = int(round(haversine_distance(lat_i, lon_i, lat_j, lon_j) * 1000))

    manager = pywrapcp.RoutingIndexManager(num_nodes, 1, [0], [dummy_end_node])
    routing = pywrapcp.RoutingModel(manager)

    def distance_callback(from_index, to_index):
        return distance_matrix[manager.IndexToNode(from_index)][manager.IndexToNode(to_index)]

    routing.SetArcCostEvaluatorOfAllVehicles(routing.RegisterTransitCallback(distance_callback))

    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    search_parameters.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    search_parameters.time_limit.FromSeconds(_time_limit_for(n))

    solution = routing.SolveWithParameters(search_parameters)
    if solution is None:
        logger.error("OR-Tools found no solution for this batch; falling back to original row order.")
        return list(rows)

    ordered_rows = []
    index = routing.Start(0)
    while not routing.IsEnd(index):
        node = manager.IndexToNode(index)
        if 1 <= node <= n:
            ordered_rows.append(rows[node - 1])
        index = solution.Value(routing.NextVar(index))
    return ordered_rows
