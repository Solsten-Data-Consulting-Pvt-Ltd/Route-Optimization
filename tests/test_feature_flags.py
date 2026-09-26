"""Tests for the dynamic feature-flag store (app/db/feature_flags.py)
without Firestore credentials — see .specify/spec.md §5.6."""

import os
import unittest
from unittest.mock import patch

# app.config reads these while app.db.feature_flags imports the shared client.
os.environ.setdefault("GOOGLE_MAPS_API_KEY", "test-key")
os.environ.setdefault("BQ_PROJECT", "test-project")
os.environ.setdefault("BQ_DATASET", "test_dataset")
os.environ.setdefault("BQ_TABLE", "test_table")
os.environ.setdefault("BQ_STRUCTURED_TABLE", "test_structured_table")
os.environ.setdefault("FIRESTORE_PROJECT", "test-project")

from app.db import feature_flags


class _FakeDoc:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    def to_dict(self):
        # Real Firestore returns a fresh dict per call, not a live reference
        # into whatever the test happens to hold -- match that so a store
        # mutation after a read can't leak into an already-cached snapshot.
        return dict(self._data) if self._data is not None else None


class _FakeSnapshot:
    def __init__(self, data):
        self.exists = data is not None
        self._data = data

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _FakeDocRef:
    def __init__(self, store, doc_id):
        self._store = store
        self._doc_id = doc_id

    def get(self):
        return _FakeSnapshot(self._store.get(self._doc_id))

    def set(self, payload):
        self._store[self._doc_id] = payload


class _FakeCollection:
    def __init__(self, store):
        self._store = store

    def document(self, doc_id):
        return _FakeDocRef(self._store, doc_id)

    def stream(self):
        return [_FakeDoc(doc_id, data) for doc_id, data in self._store.items()]


class _FakeClient:
    def __init__(self, store):
        self._store = store

    def collection(self, _name):
        return _FakeCollection(self._store)


class _BrokenClient:
    def collection(self, _name):
        raise RuntimeError("Firestore unavailable")


class FeatureFlagTests(unittest.TestCase):
    def setUp(self):
        feature_flags.invalidate_snapshot()

    def tearDown(self):
        feature_flags.invalidate_snapshot()

    def test_unknown_flag_defaults_to_off(self):
        with patch("app.db.feature_flags.get_fs_client", return_value=_FakeClient({})):
            self.assertFalse(feature_flags.is_enabled("does_not_exist"))

    def test_explicit_default_used_for_unknown_flag(self):
        with patch("app.db.feature_flags.get_fs_client", return_value=_FakeClient({})):
            self.assertTrue(feature_flags.is_enabled("does_not_exist", default=True))

    def test_enabled_flag_reads_true(self):
        store = {"outlier_trap_v2": {"enabled": True, "version": 1}}
        with patch("app.db.feature_flags.get_fs_client", return_value=_FakeClient(store)):
            self.assertTrue(feature_flags.is_enabled("outlier_trap_v2"))

    def test_disabled_flag_reads_false(self):
        store = {"outlier_trap_v2": {"enabled": False, "version": 1}}
        with patch("app.db.feature_flags.get_fs_client", return_value=_FakeClient(store)):
            self.assertFalse(feature_flags.is_enabled("outlier_trap_v2"))

    def test_get_value_reads_nested_threshold(self):
        store = {
            "outlier_trap_v2": {
                "enabled": True,
                "thresholds_km": {"suggest": 2, "medium": 5, "block": 10},
            }
        }
        with patch("app.db.feature_flags.get_fs_client", return_value=_FakeClient(store)):
            thresholds = feature_flags.get_value("outlier_trap_v2", "thresholds_km")
        self.assertEqual(thresholds, {"suggest": 2, "medium": 5, "block": 10})

    def test_snapshot_is_cached_within_ttl(self):
        store = {"master_waypoint_v2": {"enabled": False}}
        client = _FakeClient(store)
        with patch("app.db.feature_flags.get_fs_client", return_value=client):
            self.assertFalse(feature_flags.is_enabled("master_waypoint_v2"))
            # Flip the store directly, bypassing the module -- the cached
            # snapshot must not see this until the TTL/invalidation clears it.
            store["master_waypoint_v2"]["enabled"] = True
            self.assertFalse(feature_flags.is_enabled("master_waypoint_v2"))
            feature_flags.invalidate_snapshot()
            self.assertTrue(feature_flags.is_enabled("master_waypoint_v2"))

    def test_transient_read_failure_keeps_prior_snapshot(self):
        store = {"master_waypoint_v2": {"enabled": True}}
        with patch("app.db.feature_flags.get_fs_client", return_value=_FakeClient(store)):
            self.assertTrue(feature_flags.is_enabled("master_waypoint_v2"))

        # Simulate the TTL expiring (unlike invalidate_snapshot(), this does
        # NOT wipe the last-known-good data) and Firestore then failing.
        feature_flags._snapshot["loaded_at"] = 0.0
        with patch("app.db.feature_flags.get_fs_client", return_value=_BrokenClient()):
            self.assertTrue(feature_flags.is_enabled("master_waypoint_v2"))

    def test_cold_start_read_failure_defaults_all_off_not_a_crash(self):
        with patch("app.db.feature_flags.get_fs_client", return_value=_BrokenClient()):
            self.assertFalse(feature_flags.is_enabled("master_waypoint_v2"))

    def test_seed_default_flags_never_overwrites_an_existing_doc(self):
        store = {"master_waypoint_v2": {"enabled": True, "version": 99}}
        with patch("app.db.feature_flags.get_fs_client", return_value=_FakeClient(store)):
            created = feature_flags.seed_default_flags()

        self.assertEqual(created, len(feature_flags.DEFAULT_FLAGS) - 1)
        self.assertEqual(store["master_waypoint_v2"], {"enabled": True, "version": 99})
        for name in feature_flags.DEFAULT_FLAGS:
            self.assertIn(name, store)
        newly_seeded = [n for n in feature_flags.DEFAULT_FLAGS if n != "master_waypoint_v2"]
        for name in newly_seeded:
            self.assertFalse(store[name]["enabled"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
