"""Tests for the master-waypoint tier (app/db/master_waypoint.py) without
Firestore credentials — see .specify/spec.md §5.3."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("GOOGLE_MAPS_API_KEY", "test-key")
os.environ.setdefault("BQ_PROJECT", "test-project")
os.environ.setdefault("BQ_DATASET", "test_dataset")
os.environ.setdefault("BQ_TABLE", "test_table")
os.environ.setdefault("BQ_STRUCTURED_TABLE", "test_structured_table")
os.environ.setdefault("FIRESTORE_PROJECT", "test-project")

from app.db import master_waypoint


class _FakeDoc:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    def to_dict(self):
        # Match real Firestore: a fresh dict per call, not a live reference.
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

    def set(self, payload, merge=False):
        if merge and self._doc_id in self._store:
            merged = dict(self._store[self._doc_id])
            merged.update(payload)
            self._store[self._doc_id] = merged
        else:
            self._store[self._doc_id] = payload


class _FakeQuery:
    def __init__(self, items):
        self._items = items  # list of (doc_id, data)

    def where(self, field, _op, value):
        return _FakeQuery([(i, d) for i, d in self._items if d.get(field) == value])

    def limit(self, n):
        return _FakeQuery(self._items[:n])

    def stream(self):
        return [_FakeDoc(doc_id, data) for doc_id, data in self._items]


class _FakeCollection:
    def __init__(self, store):
        self._store = store

    def document(self, doc_id):
        return _FakeDocRef(self._store, doc_id)

    def where(self, field, op, value):
        return _FakeQuery(list(self._store.items())).where(field, op, value)


class _FakeClient:
    def __init__(self, store):
        self._store = store

    def collection(self, _name):
        return _FakeCollection(self._store)


class NormalizeAliasTests(unittest.TestCase):
    def test_reuses_geocache_city_canonicalization(self):
        # Same canonicalization table geocache.normalize_address applies --
        # this must not be a second, divergent implementation.
        self.assertIn("bengaluru", master_waypoint.normalize_alias("Prestige Tech Park, Bangalore"))

    def test_empty_input(self):
        self.assertEqual(master_waypoint.normalize_alias(""), "")


class LookupTests(unittest.TestCase):
    def test_miss_on_empty_store(self):
        with patch("app.db.master_waypoint.get_fs_client", return_value=_FakeClient({})):
            self.assertIsNone(master_waypoint.lookup("Sobha Dream Acres"))

    def test_hit_on_verified_entry(self):
        alias_norm = master_waypoint.normalize_alias("Sobha Dream Acres")
        store = {
            alias_norm: {
                "alias_normalized": alias_norm,
                "latitude": 12.9, "longitude": 77.6,
                "verified": True,
                "formatted_address": "Sobha Dream Acres, Bengaluru",
            }
        }
        with patch("app.db.master_waypoint.get_fs_client", return_value=_FakeClient(store)):
            result = master_waypoint.lookup("Sobha Dream Acres")

        self.assertIsNotNone(result)
        self.assertEqual(result["latitude"], 12.9)
        self.assertEqual(result["longitude"], 77.6)
        self.assertEqual(result["source"], "master_waypoint")

    def test_unverified_entry_is_not_a_hit(self):
        alias_norm = master_waypoint.normalize_alias("Sobha Dream Acres")
        store = {
            alias_norm: {
                "alias_normalized": alias_norm,
                "latitude": 12.9, "longitude": 77.6,
                "verified": False,
            }
        }
        with patch("app.db.master_waypoint.get_fs_client", return_value=_FakeClient(store)):
            self.assertIsNone(master_waypoint.lookup("Sobha Dream Acres"))

    def test_lookup_failure_returns_none_not_a_crash(self):
        class _BrokenClient:
            def collection(self, _name):
                raise RuntimeError("Firestore unavailable")

        with patch("app.db.master_waypoint.get_fs_client", return_value=_BrokenClient()):
            self.assertIsNone(master_waypoint.lookup("Sobha Dream Acres"))


class SeedTests(unittest.TestCase):
    def test_seed_creates_a_verified_entry(self):
        store = {}
        with patch("app.db.master_waypoint.get_fs_client", return_value=_FakeClient(store)):
            master_waypoint.seed("Manyata Tech Park", latitude=13.0, longitude=77.6)

        alias_norm = master_waypoint.normalize_alias("Manyata Tech Park")
        self.assertTrue(store[alias_norm]["verified"])
        self.assertEqual(store[alias_norm]["latitude"], 13.0)

    def test_seed_refuses_to_overwrite_a_verified_entry(self):
        alias_norm = master_waypoint.normalize_alias("Prestige Tech Park")
        store = {
            alias_norm: {
                "alias_normalized": alias_norm,
                "latitude": 1.0, "longitude": 1.0,
                "verified": True,
            }
        }
        with patch("app.db.master_waypoint.get_fs_client", return_value=_FakeClient(store)):
            master_waypoint.seed("Prestige Tech Park", latitude=99.0, longitude=99.0)

        self.assertEqual(store[alias_norm]["latitude"], 1.0)


class RecordConfirmationTests(unittest.TestCase):
    def test_promotes_on_third_distinct_consignment(self):
        store = {}
        with patch("app.db.master_waypoint.get_fs_client", return_value=_FakeClient(store)):
            r1 = master_waypoint.record_confirmation("New Tower", 12.0, 77.0, consignment_id="C1")
            r2 = master_waypoint.record_confirmation("New Tower", 12.0, 77.0, consignment_id="C2")
            r3 = master_waypoint.record_confirmation("New Tower", 12.0, 77.0, consignment_id="C3")

        self.assertFalse(r1["promoted"])
        self.assertFalse(r2["promoted"])
        self.assertTrue(r3["promoted"])

        alias_norm = master_waypoint.normalize_alias("New Tower")
        self.assertTrue(store[alias_norm]["verified"])
        self.assertEqual(store[alias_norm]["latitude"], 12.0)

    def test_same_consignment_confirming_twice_does_not_double_count(self):
        store = {}
        with patch("app.db.master_waypoint.get_fs_client", return_value=_FakeClient(store)):
            master_waypoint.record_confirmation("New Tower", 12.0, 77.0, consignment_id="C1")
            r2 = master_waypoint.record_confirmation("New Tower", 12.0, 77.0, consignment_id="C1")

        self.assertFalse(r2["promoted"])
        self.assertEqual(r2["reason"], "duplicate_confirmation")

    def test_confirmations_at_different_coordinates_do_not_share_a_count(self):
        store = {}
        with patch("app.db.master_waypoint.get_fs_client", return_value=_FakeClient(store)):
            master_waypoint.record_confirmation("New Tower", 12.00000, 77.00000, consignment_id="C1")
            master_waypoint.record_confirmation("New Tower", 13.00000, 78.00000, consignment_id="C2")
            r3 = master_waypoint.record_confirmation("New Tower", 12.00000, 77.00000, consignment_id="C3")

        # Only 2 distinct confirmations agree on (12.0, 77.0) -- not promoted yet.
        self.assertFalse(r3["promoted"])
        self.assertEqual(r3["distinct_count"], 2)

    def test_confirmation_against_already_verified_entry_is_ignored(self):
        alias_norm = master_waypoint.normalize_alias("New Tower")
        store = {
            alias_norm: {
                "alias_normalized": alias_norm,
                "latitude": 5.0, "longitude": 5.0,
                "verified": True,
            }
        }
        with patch("app.db.master_waypoint.get_fs_client", return_value=_FakeClient(store)):
            result = master_waypoint.record_confirmation("New Tower", 99.0, 99.0, consignment_id="C1")

        self.assertFalse(result["promoted"])
        self.assertEqual(result["reason"], "already_verified")
        self.assertEqual(store[alias_norm]["latitude"], 5.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
