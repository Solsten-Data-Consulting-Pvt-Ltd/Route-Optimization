"""OSRM road distances (featureFlags/roadDistanceProvider), alerts, health check.

OSRM and the Google Chat webhook are replaced by fakes; nothing leaves the
machine. Same 11 -> 12 -> 13 fake road network as test_road_distance.py.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import tests  # noqa: F401  env vars + offline feature flags (tests/__init__.py)

from fastapi.testclient import TestClient

from app import config
from app.db import feature_flags
from app.services import alerts, osrm, road_matrix, sorting
from app.services.tsp import haversine_distance
from tests.test_road_distance import HUB, STOPS, FakeRoutesApi, MemoryCache, Patched, road, route, stops


def parse_coords(path):
    body = path.split("/driving/", 1)[1]
    return [tuple(float(x) for x in c.split(",")) for c in body.split(";")]   # (lon, lat)


class FakeOsrm:
    """Stands in for requests.get to an OSRM server (/table and /route)."""

    def __init__(self, fail=None, null_pairs=(), snap=None, scale=1.0, bad_code=None):
        self.calls, self.fail, self.null_pairs = [], fail, set(null_pairs)
        self.snap = snap or {}            # (lat, lon) -> snap distance
        self.scale, self.bad_code = scale, bad_code

    def __call__(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        if self.fail:
            raise self.fail
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        path = url.split(config.OSRM_BASE_URL, 1)[1]
        coords = [(lat, lon) for lon, lat in parse_coords(path)]
        if self.bad_code:
            resp.json.return_value = {"code": self.bad_code}
            return resp
        if "/route/" in path:
            d = road(coords[0], coords[1]) * self.scale
            resp.json.return_value = {"code": "Ok", "routes": [{"distance": d}]}
            return resp
        params = params or {}
        n = len(coords)
        src = [int(x) for x in params["sources"].split(";")] if "sources" in params else list(range(n))
        dst = [int(x) for x in params["destinations"].split(";")] if "destinations" in params else list(range(n))
        rows = [[None if (coords[i], coords[j]) in self.null_pairs else
                 (0 if i == j else road(coords[i], coords[j]) * self.scale) for j in dst] for i in src]
        wp = lambda k: {"distance": self.snap.get(coords[k], 5.0), "location": list(coords[k])[::-1]}
        resp.json.return_value = {"code": "Ok", "distances": rows,
                                  "sources": [wp(i) for i in src], "destinations": [wp(j) for j in dst]}
        return resp


def points_of(names=STOPS):
    return [HUB] + list(names.values())


class OsrmEnv:
    """OSRM on, pointed at a fake; Google must not be called."""

    def __init__(self, fake=None, provider="osrm"):
        self.fake = fake or FakeOsrm()
        self.provider = provider
        self.google = FakeRoutesApi()
        self.chat = MagicMock()

    def _post(self, url, **kwargs):
        if url.startswith("https://chat.test/"):
            return self.chat(url, **kwargs)
        return self.google(url, **kwargs)

    def __enter__(self):
        alerts.reset()
        self.ps = [
            patch.object(config, "OSRM_BASE_URL", "http://osrm.test:5000"),
            patch.object(config, "ALERT_CHAT_WEBHOOK_URL", "https://chat.test/hook"),
            patch.object(config, "ALERT_ENV_NAME", "test"),
            patch.object(osrm.requests, "get", side_effect=self.fake),
            # One `requests` module serves Google, OSRM and the Chat webhook:
            # post() goes to the fake Google API or the fake webhook by URL.
            patch.object(road_matrix.requests, "post", side_effect=self._post),
            patch.object(feature_flags, "road_distance_provider", return_value=self.provider),
            patch.object(road_matrix, "cache_get", return_value={}),
            patch.object(road_matrix, "cache_put"),
        ]
        for p in self.ps:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self.ps:
            p.stop()


class TableTests(unittest.TestCase):
    def test_coordinates_are_sent_lon_lat(self):
        with OsrmEnv() as env:
            osrm.table(points_of())
        url = env.fake.calls[0][0]
        self.assertIn("77.6400000,12.9000000;", url)       # HUB = (12.900, 77.640)

    def test_one_request_for_a_normal_drs_and_distances_match(self):
        pts = points_of()
        with OsrmEnv() as env:
            distances, snaps, made = osrm.table(pts)
        self.assertEqual(made, 1)
        self.assertAlmostEqual(distances[1][2], road(pts[1], pts[2]), places=3)
        self.assertEqual(snaps, [5.0] * len(pts))

    def test_big_drs_is_split_into_blocks_and_stitched_back(self):
        pts = [(12.9 + 0.001 * (i % 13), 77.6 + 0.001 * (i // 13)) for i in range(230)]
        with patch.object(config, "OSRM_TABLE_BLOCK", 100):
            with OsrmEnv() as env:
                distances, snaps, made = osrm.table(pts)
                sizes = [len(parse_coords(url.split(config.OSRM_BASE_URL, 1)[1]))
                         for url, _params in env.fake.calls]
        self.assertEqual(made, 9)                                  # 3 x 3 blocks of 100/100/30
        for i, j in [(0, 229), (150, 3), (229, 0), (99, 100), (120, 121)]:
            self.assertAlmostEqual(distances[i][j], road(pts[i], pts[j]), places=3)
        self.assertTrue(all(s is not None for s in snaps))
        self.assertLessEqual(max(sizes), 200)

    def test_http_error_raises_osrm_error(self):
        with OsrmEnv(FakeOsrm(fail=osrm.requests.ConnectionError("refused"))):
            with self.assertRaises(osrm.OsrmError):
                osrm.table(points_of())

    def test_osrm_error_code_raises(self):
        with OsrmEnv(FakeOsrm(bad_code="InvalidQuery")):
            with self.assertRaises(osrm.OsrmError):
                osrm.table(points_of())

    def test_missing_base_url_raises(self):
        with patch.object(config, "OSRM_BASE_URL", ""):
            with self.assertRaises(osrm.OsrmError):
                osrm.table(points_of())


class MatrixTests(unittest.TestCase):
    def test_every_pair_comes_from_osrm_and_google_is_never_called(self):
        pts = points_of()
        with OsrmEnv() as env:
            matrix, stats = osrm.build_distance_matrix_osrm(pts)
        self.assertEqual(env.google.calls, [])
        n = len(pts)
        self.assertEqual((stats["road_pairs"], stats["estimated_pairs"]), (n * (n - 1), 0))
        self.assertEqual((stats["provider"], stats["from_api"], stats["osrm_errors"]), ("osrm", 0, 0))
        self.assertEqual(matrix[1][2], int(round(road(pts[1], pts[2]))))
        self.assertEqual(matrix[3][3], 0)

    def test_pair_osrm_cannot_route_uses_the_estimate(self):
        pts = points_of()
        a, b = pts[1], pts[2]
        with OsrmEnv(FakeOsrm(null_pairs=[(a, b)])):
            matrix, stats = osrm.build_distance_matrix_osrm(pts)
        self.assertEqual(stats["osrm_unroutable_pairs"], 1)
        self.assertEqual(stats["estimated_pairs"], 1)
        self.assertGreater(matrix[1][2], 0)                      # estimated, not 0 or None

    def test_stop_placed_far_from_any_road_uses_the_estimate(self):
        pts = points_of()
        far = pts[2]
        with OsrmEnv(FakeOsrm(snap={far: 450.0})):
            matrix, stats = osrm.build_distance_matrix_osrm(pts)
        self.assertEqual(stats["snap_outliers"], 1)
        n = len(pts)
        self.assertEqual(stats["estimated_pairs"], 2 * (n - 1))      # its row and its column
        self.assertGreater(matrix[0][2], 0)

    def test_osrm_down_raises(self):
        with OsrmEnv(FakeOsrm(fail=osrm.requests.Timeout("slow"))):
            with self.assertRaises(osrm.OsrmError):
                osrm.build_distance_matrix_osrm(points_of())


class FlagTests(unittest.TestCase):
    def _provider(self, flags):
        client = MagicMock()
        client.collection.return_value.stream.return_value = iter(
            [SimpleNamespace(id=k, to_dict=lambda v=v: v) for k, v in flags.items()])
        with patch.object(feature_flags, "get_fs_client", return_value=client):
            feature_flags.reset_cache()
            try:
                return feature_flags.road_distance_provider()
            finally:
                feature_flags.reset_cache()

    def test_values(self):
        for value in ("google", "shadow", "osrm"):
            self.assertEqual(self._provider({"roadDistanceProvider": {"value": value}}), value)

    def test_default_is_google(self):
        self.assertEqual(self._provider({}), "google")
        self.assertEqual(self._provider({"roadDistanceProvider": {}}), "google")
        self.assertEqual(self._provider({"roadDistanceProvider": {"value": "OSRM "}}), "google")
        self.assertEqual(self._provider({"roadDistanceProvider": {"value": 5}}), "google")

    def test_read_error_is_google(self):
        client = MagicMock()
        client.collection.side_effect = RuntimeError("firestore down")
        with patch.object(feature_flags, "get_fs_client", return_value=client):
            feature_flags.reset_cache()
            self.assertEqual(feature_flags.road_distance_provider(), "google")
            feature_flags.reset_cache()


class SortingProviderTests(unittest.TestCase):
    def test_osrm_provider_orders_the_route_without_google(self):
        with OsrmEnv() as env:
            seq, stats = route(use_road=True)
        self.assertEqual(env.google.calls, [])
        self.assertLess(seq.index("S13"), seq.index("S12"))        # the 11 -> 12 -> 13 fix, via OSRM
        self.assertEqual((stats["distance_source"], stats["provider"]), ("road", "osrm"))

    def test_osrm_down_plans_on_straight_lines_and_alerts_once(self):
        with OsrmEnv(FakeOsrm(fail=osrm.requests.ConnectionError("refused"))) as env:
            seq, stats = route(use_road=True)
            route(use_road=True)                                   # a second DRS in the same minute
        self.assertEqual(stats["distance_source"], "straight_line")
        self.assertEqual((stats["provider"], stats["osrm_errors"]), ("osrm", 1))
        self.assertEqual(env.google.calls, [])                     # still no Google
        self.assertEqual(env.chat.call_count, 1)                   # cooldown: one message
        self.assertIn("OSRM could not answer", env.chat.call_args.kwargs["json"]["text"])
        self.assertTrue(env.chat.call_args.kwargs["json"]["text"].startswith("[test] "))
        straight_seq, _ = route(use_road=False)
        self.assertEqual(seq, straight_seq)

    def test_google_provider_unchanged(self):
        with Patched():
            seq, stats = route(use_road=True)
        self.assertEqual((stats["distance_source"], stats["provider"]), ("road", "google"))
        self.assertGreater(stats["from_api"], 0)

    def test_shadow_routes_on_google_and_logs_osrm_gap(self):
        with OsrmEnv(provider="shadow") as env:
            seq, stats = route(use_road=True)
        self.assertGreater(len(env.google.calls), 0)               # Google still builds the route
        self.assertEqual(stats["provider"], "shadow")
        self.assertEqual(stats["distance_source"], "road")
        self.assertGreater(stats["shadow_pairs"], 0)
        self.assertEqual(stats["shadow_off_pairs"], 0)             # fake OSRM == fake Google (1.0 vs 1.2 scale)
        self.assertIn("shadow_median_ratio", stats)

    def test_shadow_counts_pairs_where_osrm_is_far_off(self):
        # OSRM reports 3x Google's distance -> every pair over 25% and 500 m is "off".
        with OsrmEnv(FakeOsrm(scale=3.0), provider="shadow"):
            _, stats = route(use_road=True)
        self.assertGreater(stats["shadow_off_pairs"], 0)
        self.assertGreater(stats["shadow_off_pct"], 50)

    def test_shadow_survives_osrm_being_down(self):
        with OsrmEnv(FakeOsrm(fail=osrm.requests.ConnectionError("refused")), provider="shadow") as env:
            seq, stats = route(use_road=True)
        self.assertGreater(len(env.google.calls), 0)
        self.assertEqual(stats["distance_source"], "road")         # route unaffected
        self.assertIn("shadow_error", stats)
        env.chat.assert_not_called()                               # shadow problems do not page anyone

    def test_shadow_route_equals_google_route(self):
        with Patched():
            google_seq, _ = route(use_road=True)
        with OsrmEnv(provider="shadow"):
            shadow_seq, _ = route(use_road=True)
        self.assertEqual(google_seq, shadow_seq)


class AlertTests(unittest.TestCase):
    def test_posts_to_chat_with_env_label(self):
        with OsrmEnv() as env:
            self.assertTrue(alerts.send_alert("k", "hello"))
        env.chat.assert_called_once()
        self.assertEqual(env.chat.call_args.kwargs["json"], {"text": "[test] hello"})

    def test_cooldown_and_force(self):
        with OsrmEnv() as env:
            self.assertTrue(alerts.send_alert("k", "one"))
            self.assertFalse(alerts.send_alert("k", "two"))
            self.assertTrue(alerts.send_alert("k", "three", force=True))
            self.assertTrue(alerts.send_alert("other", "four"))
        self.assertEqual(env.chat.call_count, 3)

    def test_no_webhook_only_logs(self):
        alerts.reset()
        with patch.object(config, "ALERT_CHAT_WEBHOOK_URL", ""), \
                patch.object(alerts.requests, "post") as post:
            self.assertFalse(alerts.send_alert("k", "x"))
        post.assert_not_called()

    def test_webhook_failure_never_raises(self):
        alerts.reset()
        with patch.object(config, "ALERT_CHAT_WEBHOOK_URL", "https://chat.test/hook"), \
                patch.object(alerts.requests, "post", side_effect=alerts.requests.ConnectionError("x")):
            self.assertFalse(alerts.send_alert("k", "x"))


class HealthTests(unittest.TestCase):
    def setUp(self):
        from app.main import app
        self.client = TestClient(app)

    def test_ok(self):
        with OsrmEnv() as env:
            r = self.client.get("/health/osrm")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")
        env.chat.assert_not_called()                               # quiet unless ?notify

    def test_notify_posts_a_daily_ok(self):
        with OsrmEnv() as env:
            r = self.client.get("/health/osrm?notify=true")
        self.assertEqual(r.status_code, 200)
        self.assertIn("health check OK", env.chat.call_args.kwargs["json"]["text"])

    def test_down_is_503_and_alerts_every_time(self):
        with OsrmEnv(FakeOsrm(fail=osrm.requests.ConnectionError("refused"))) as env:
            r1 = self.client.get("/health/osrm")
            r2 = self.client.get("/health/osrm")
        self.assertEqual((r1.status_code, r2.status_code), (503, 503))
        self.assertEqual(r1.json()["status"], "failed")
        self.assertEqual(env.chat.call_count, 2)                   # forced: the 7 AM check is never silent
        self.assertIn("FAILED", env.chat.call_args.kwargs["json"]["text"])

    def test_wrong_distance_is_a_failure(self):
        with OsrmEnv(FakeOsrm(scale=0.1)):
            r = self.client.get("/health/osrm")
        self.assertEqual(r.status_code, 503)
        self.assertIn("expected about", r.json()["detail"])

    def test_osrm_not_configured_is_disabled_not_failed(self):
        with patch.object(config, "OSRM_BASE_URL", ""):
            r = self.client.get("/health/osrm")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "disabled")


if __name__ == "__main__":
    unittest.main()
