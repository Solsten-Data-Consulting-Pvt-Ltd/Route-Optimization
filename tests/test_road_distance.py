"""Road distances for route sorting (featureFlags/roadDistanceRouting).

The 11 -> 12 -> 13 case: 12 is close to 11 in a straight line but can only be
reached by road through 13 (a lake/field in between). Google's Routes API is
replaced by a fake road network; the Firestore cache by a dict.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import tests  # noqa: F401  env vars + offline feature flags (tests/__init__.py)

from app.services import road_matrix, sorting
from app.services.tsp import haversine_distance, solve_route_order

HUB = (12.900, 77.640)
STOPS = {
    "S10": (12.900, 77.690),
    "S11": (12.900, 77.700),
    "S12": (12.904, 77.705),     # 700 m from S11 in a straight line ...
    "S13": (12.898, 77.712),     # ... but by road only reachable through S13
    "S14": (12.905, 77.722),
}


def straight(a, b):
    return haversine_distance(a[0], a[1], b[0], b[1]) * 1000.0


def road(a, b):
    """Fake road network: S12 sits behind a lake; from the west you must go
    round through S13."""
    s12, s13 = STOPS["S12"], STOPS["S13"]
    west = (a[1] < s13[1] and a != s12) if b == s12 else (b[1] < s13[1] and b != s12) if a == s12 else False
    if west:
        other = a if b == s12 else b
        return (straight(other, s13) + straight(s13, s12)) * 1.2
    return straight(a, b) * 1.2


class FakeRoutesApi:
    """Stands in for requests.post to the Compute Route Matrix endpoint."""

    def __init__(self, fail=False, no_route=False):
        self.calls, self.fail, self.no_route = [], fail, no_route

    def __call__(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "body": json})
        if self.fail:
            raise road_matrix.requests.ConnectionError("Routes API disabled")
        ll = lambda w: (w["waypoint"]["location"]["latLng"]["latitude"],
                        w["waypoint"]["location"]["latLng"]["longitude"])
        origins = [ll(w) for w in json["origins"]]
        dests = [ll(w) for w in json["destinations"]]
        out = []
        for oi, o in enumerate(origins):
            for di, d in enumerate(dests):
                if self.no_route:
                    out.append({"originIndex": oi, "destinationIndex": di, "condition": "ROUTE_NOT_FOUND"})
                else:
                    out.append({"originIndex": oi, "destinationIndex": di, "condition": "ROUTE_EXISTS",
                                "distanceMeters": int(road(o, d)), "duration": f"{int(road(o, d) / 8)}s"})
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = out
        return resp


class MemoryCache:
    def __init__(self):
        self.store = {}

    def get(self, keys):
        return {k: self.store[k] for k in keys if k in self.store}

    def put(self, values):
        self.store.update({k: v[0] for k, v in values.items()})


def stops(names=STOPS):
    return [SimpleNamespace(sorting_id=f"s-{k}", consignmentId=k, status="Out_For_Delivery",
                            latitude=v[0], longitude=v[1], geohash_exact_loc="x",
                            geohash_locality_loc="tdr1x", actual_sequence_order=None)
            for k, v in names.items()]


class Patched:
    """Patch the API and the cache for one block."""

    def __init__(self, api=None, cache=None):
        self.api = api or FakeRoutesApi()
        self.cache = cache or MemoryCache()

    def __enter__(self):
        self.ps = [patch.object(road_matrix.requests, "post", side_effect=self.api),
                   patch.object(road_matrix, "cache_get", side_effect=self.cache.get),
                   patch.object(road_matrix, "cache_put", side_effect=self.cache.put)]
        for p in self.ps:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self.ps:
            p.stop()


def route(use_road, rows=None):
    stats = {}
    updates = sorting.compute_groups_and_sequence(
        rows or stops(), "D1", {"latitude": HUB[0], "longitude": HUB[1], "starting_address": "Hub"},
        use_road=use_road, stats_out=stats)
    seq = [u["sorting_id"][2:] for u in sorted(updates, key=lambda u: u["actual_sequence_order"])]
    return seq, stats


class ElevenTwelveThirteenTests(unittest.TestCase):
    def test_straight_lines_reproduce_the_problem(self):
        seq, _ = route(use_road=False)
        self.assertEqual(seq[seq.index("S11") + 1], "S12")        # 11 -> 12 -> 13

    def test_road_distance_fixes_it(self):
        with Patched():
            seq, stats = route(use_road=True)
        self.assertNotEqual(seq[seq.index("S11") + 1], "S12")     # no detour past 13
        self.assertLess(seq.index("S13"), seq.index("S12"))
        self.assertEqual(stats["distance_source"], "road")

    def test_road_order_is_shorter_by_road(self):
        with Patched():
            road_seq, _ = route(use_road=True)
        straight_seq, _ = route(use_road=False)

        def road_km(seq):
            pts = [HUB] + [STOPS[s] for s in seq]
            return sum(road(pts[i], pts[i + 1]) for i in range(len(pts) - 1)) / 1000
        self.assertLess(road_km(road_seq), road_km(straight_seq))


class CostControlTests(unittest.TestCase):
    def many(self, n=20):
        return {f"C{i:02d}": (12.88 + (i % 5) * 0.01, 77.62 + (i // 5) * 0.012) for i in range(n)}

    def test_small_drs_gets_every_pair_by_road_nothing_guessed(self):
        pts = [HUB] + list(self.many().values())   # 21 points, all in different cells
        with Patched() as p:
            m, stats = road_matrix.build_distance_matrix(pts)
        self.assertEqual(stats["mode"], "full")
        self.assertEqual(stats["road_pairs"], len(pts) * (len(pts) - 1))
        self.assertEqual(stats["estimated_pairs"], 0)
        for call in p.api.calls:
            self.assertEqual(len(call["body"]["origins"]), 1)
            self.assertLessEqual(len(call["body"]["destinations"]),
                                 road_matrix.ROAD_MAX_DESTINATIONS_PER_REQUEST)
        self.assertEqual(m[0][1], int(road(pts[0], pts[1])))

    def test_large_drs_asks_nearest_neighbours_in_both_directions(self):
        pts = [HUB] + list(self.many(30).values())
        with patch.object(road_matrix, "ROAD_FULL_MATRIX_MAX_POINTS", 10), Patched() as p:
            _, stats = road_matrix.build_distance_matrix(pts)
        self.assertEqual(stats["mode"], "nearest")
        asked = {(tuple(c["body"]["origins"][0]["waypoint"]["location"]["latLng"].values()),
                  tuple(d["waypoint"]["location"]["latLng"].values()))
                 for c in p.api.calls for d in c["body"]["destinations"]}
        for a, b in list(asked):
            self.assertIn((b, a), asked)
        self.assertLess(stats["api_elements"], len(pts) * (len(pts) - 1))

    def test_guesses_use_this_drs_road_ratio_not_a_fixed_1_4(self):
        # The DHRD20055 case: roads run ~2x straight line; a far pair guessed
        # at 1.4x looked cheaper than real roads and the solver jumped through it.
        pts = [HUB] + list(self.many(30).values())
        class TwiceStraight(FakeRoutesApi):
            def __call__(self, url, headers=None, json=None, timeout=None):
                resp = super().__call__(url, headers, json, timeout)
                for el in resp.json.return_value:
                    el["distanceMeters"] = int(el["distanceMeters"] / 1.2 * 2.0)
                return resp
        with patch.object(road_matrix, "ROAD_FULL_MATRIX_MAX_POINTS", 10), \
             Patched(api=TwiceStraight()):
            m, stats = road_matrix.build_distance_matrix(pts)
        self.assertAlmostEqual(stats["detour_factor"], 2.0, places=1)
        far = max(range(1, len(pts)), key=lambda j: straight(pts[0], pts[j]))
        self.assertGreater(m[0][far], straight(pts[0], pts[far]) * 1.9)

    def test_second_run_is_served_from_cache(self):
        pts = [HUB] + list(self.many().values())
        cache = MemoryCache()
        with Patched(cache=cache):
            _, first = road_matrix.build_distance_matrix(pts)
        with Patched(cache=cache) as p:
            _, second = road_matrix.build_distance_matrix(pts)
        self.assertGreater(first["from_api"], 0)
        self.assertEqual((second["from_api"], len(p.api.calls)), (0, 0))
        self.assertEqual(second["from_cache"], first["from_api"])

    def test_element_cap_is_respected(self):
        pts = [HUB] + list(self.many().values())
        with Patched():
            _, stats = road_matrix.build_distance_matrix(pts, max_elements=20)
        self.assertLessEqual(stats["api_elements"], 20)

    def test_request_is_essentials_drive_without_traffic(self):
        with Patched() as p:
            road_matrix.build_distance_matrix([HUB, STOPS["S10"], STOPS["S11"]])
        call = p.api.calls[0]
        self.assertEqual(call["url"], road_matrix.ROUTES_MATRIX_URL)
        self.assertEqual(call["body"]["travelMode"], "DRIVE")
        self.assertEqual(call["body"]["routingPreference"], "TRAFFIC_UNAWARE")
        self.assertIn("X-Goog-Api-Key", call["headers"])
        self.assertIn("distanceMeters", call["headers"]["X-Goog-FieldMask"])


class FallbackTests(unittest.TestCase):
    def test_api_down_means_straight_line_order(self):
        with Patched(api=FakeRoutesApi(fail=True)):
            seq, stats = route(use_road=True)
        self.assertEqual(seq, route(use_road=False)[0])
        self.assertEqual(stats["distance_source"], "straight_line")
        self.assertGreater(stats["api_errors"], 0)

    def test_no_route_found_uses_estimate(self):
        with Patched(api=FakeRoutesApi(no_route=True)):
            m, stats = road_matrix.build_distance_matrix([HUB, STOPS["S10"]])
        self.assertEqual(stats["road_pairs"], 0)
        self.assertEqual(m[0][1], int(round(straight(HUB, STOPS["S10"]) * 1.4)))

    def test_matrix_builder_crash_never_breaks_sorting(self):
        with patch("app.services.road_matrix.build_distance_matrix", side_effect=RuntimeError("x")):
            seq, stats = route(use_road=True)
        self.assertEqual(seq, route(use_road=False)[0])
        self.assertEqual(stats["distance_source"], "straight_line")


class TspMatrixTests(unittest.TestCase):
    def test_given_matrix_is_used(self):
        rows = [SimpleNamespace(latitude=0.0, longitude=0.001 * i, name=n)
                for i, n in enumerate(["A", "B"], start=1)]
        # By straight line A is nearer the hub; the matrix says B is.
        m = [[0, 900, 100], [900, 0, 50], [100, 50, 0]]
        self.assertEqual([r.name for r in solve_route_order(0.0, 0.0, rows)], ["A", "B"])
        self.assertEqual([r.name for r in solve_route_order(0.0, 0.0, rows, distance_matrix_m=m)],
                         ["B", "A"])


class PipelineFlagTests(unittest.TestCase):
    def run_pipeline(self, flag):
        with patch.object(sorting, "_road_distance_enabled", return_value=flag), \
             patch.object(sorting, "get_drs_starting_point",
                          return_value={"latitude": HUB[0], "longitude": HUB[1]}), \
             patch.object(sorting, "deactivate_duplicate_routing_rows"), \
             patch.object(sorting, "fetch_active_rows_for_drs", return_value=stops()), \
             patch.object(sorting, "write_group_assignments"), \
             patch.object(sorting, "fetch_assigned_rows_for_drs", return_value=[]), \
             patch.object(sorting, "upsert_routing_from_sorting"), \
             Patched() as p:
            return sorting.run_sorting_pipeline("D1"), p.api.calls

    def test_flag_off_is_todays_behaviour_and_calls_nothing(self):
        result, calls = self.run_pipeline(False)
        self.assertEqual(result, {"optimized_count": 5, "planned_from": "hub"})
        self.assertEqual(calls, [])

    def test_flag_on_reports_road_distance_use(self):
        result, calls = self.run_pipeline(True)
        self.assertEqual(result["distance_source"], "road")
        self.assertGreater(result["road_distance"]["from_api"], 0)
        self.assertTrue(calls)


if __name__ == "__main__":
    unittest.main()
