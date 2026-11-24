"""Offline OSRM vs Google comparison (Phase 2). Read-only, local only."""
import os, json, math, statistics as st, sys, urllib.request
from ortools.constraint_solver import pywrapcp, routing_enums_pb2

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
OSRM = "http://127.0.0.1:5000"


def hav(a, b):
    R = 6371000.0
    dlat = math.radians(b[0] - a[0]); dlon = math.radians(b[1] - a[1])
    x = math.sin(dlat / 2) ** 2 + math.cos(math.radians(a[0])) * math.cos(math.radians(b[0])) * math.sin(dlon / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(x), math.sqrt(1 - x))


def osrm_table(pts):
    coords = ";".join(f"{lon},{lat}" for lat, lon in pts)
    url = f"{OSRM}/table/v1/driving/{coords}?annotations=distance,duration"
    with urllib.request.urlopen(url, timeout=60) as r:
        d = json.load(r)
    assert d["code"] == "Ok", d
    snaps = [w["distance"] for w in d["sources"]]
    return d["distances"], d["durations"], snaps


def google_matrix(d):
    cells, g = d["cells"], d["google_m_by_cell_pair"]
    n = len(cells); M = [[0] * n for _ in range(n)]; missing = 0
    for i in range(n):
        for j in range(n):
            if i == j or cells[i] == cells[j]:
                continue
            v = g.get(f"{cells[i]}_{cells[j]}")
            if v is None:
                missing += 1; v = hav(d["points"][i], d["points"][j]) * 1.4
            M[i][j] = v
    return M, missing


def solve(M, secs):
    n = len(M) - 1  # stops; node 0 = hub
    end = n + 1
    D = [[0] * (n + 2) for _ in range(n + 2)]
    for i in range(n + 1):
        for j in range(n + 1):
            D[i][j] = int(M[i][j])
    man = pywrapcp.RoutingIndexManager(n + 2, 1, [0], [end])
    rm = pywrapcp.RoutingModel(man)
    cb = rm.RegisterTransitCallback(lambda a, b: D[man.IndexToNode(a)][man.IndexToNode(b)])
    rm.SetArcCostEvaluatorOfAllVehicles(cb)
    p = pywrapcp.DefaultRoutingSearchParameters()
    p.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    p.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    p.time_limit.FromSeconds(secs)
    s = rm.SolveWithParameters(p)
    order, idx = [], rm.Start(0)
    while not rm.IsEnd(idx):
        order.append(man.IndexToNode(idx)); idx = s.Value(rm.NextVar(idx))
    return order  # starts with 0


def path_len(M, order):
    return sum(M[a][b] for a, b in zip(order, order[1:]))


def pct(xs, q):
    xs = sorted(xs); return xs[min(len(xs) - 1, int(q * len(xs)))]


out = {}
for name in ["DHRD10230", "DHRD10239", "DHRD10240"]:
    d = json.load(open(f"{DATA}/{name}.json"))
    pts = d["points"]; n = len(pts) - 1
    G, missing = google_matrix(d)
    O, Odur, snaps = osrm_table(pts)
    ratios, absdiff, big = [], [], []
    for i in range(len(pts)):
        for j in range(len(pts)):
            if i == j or d["cells"][i] == d["cells"][j]:
                continue
            if f"{d['cells'][i]}_{d['cells'][j]}" not in d["google_m_by_cell_pair"]:
                continue
            g, o = G[i][j], O[i][j]
            if g <= 0 or o is None:
                continue
            r = o / g; ratios.append(r); absdiff.append(abs(o - g))
            if abs(r - 1) > 0.25 and abs(o - g) > 500:
                big.append((i, j, round(g), round(o)))
    secs = 15 if n > 50 else (3 if n > 10 else 1)
    og = solve(G, secs); oo = solve(O, secs)
    res = {
        "stops": n, "pairs": len(ratios), "google_missing_filled": missing,
        "ratio_median": round(st.median(ratios), 3),
        "ratio_p10": round(pct(ratios, 0.10), 3), "ratio_p90": round(pct(ratios, 0.90), 3),
        "abs_diff_median_m": round(st.median(absdiff)), "abs_diff_p90_m": round(pct(absdiff, 0.9)),
        "pairs_off_gt25pct_and_500m": len(big), "share_off": round(len(big) / len(ratios), 3),
        "snap_m_median": round(st.median(snaps), 1), "snap_m_max": round(max(snaps), 1),
        "snap_over_100m": [(i, round(s)) for i, s in enumerate(snaps) if s > 100],
        "route_google_solve_km": round(path_len(G, og) / 1000, 2),
        "route_osrm_solve_scored_by_google_km": round(path_len(G, oo) / 1000, 2),
        "route_osrm_solve_osrm_km": round(path_len(O, oo) / 1000, 2),
        "same_order": og == oo,
        "worst_pairs": sorted(big, key=lambda t: -abs(t[3] - t[2]))[:8],
    }
    res["route_loss_pct"] = round(100 * (res["route_osrm_solve_scored_by_google_km"] / res["route_google_solve_km"] - 1), 2)
    out[name] = res
    print(name, json.dumps(res, indent=1)); sys.stdout.flush()

json.dump(out, open("/home/claude/osrm/results.json", "w"), indent=1)
