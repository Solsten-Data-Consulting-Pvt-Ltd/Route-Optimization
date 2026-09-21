"""Tests for per-DRS cache metrics persistence without Firestore credentials."""

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

# app.config reads these while app.db.cache_metrics imports the shared client.
os.environ.setdefault("GOOGLE_MAPS_API_KEY", "test-key")
os.environ.setdefault("BQ_PROJECT", "test-project")
os.environ.setdefault("BQ_DATASET", "test_dataset")
os.environ.setdefault("BQ_TABLE", "test_table")
os.environ.setdefault("BQ_STRUCTURED_TABLE", "test_structured_table")
os.environ.setdefault("FIRESTORE_PROJECT", "test-project")

from app.db.cache_metrics import write_drs_cache_metrics


class _FakeBatch:
    def __init__(self):
        self.writes = []
        self.committed = False

    def set(self, document, payload):
        self.writes.append((document, payload))

    def commit(self):
        self.committed = True


class _FakeCollection:
    def document(self, document_id):
        return document_id


class _FakeClient:
    def __init__(self):
        self.batch_instance = _FakeBatch()

    def batch(self):
        return self.batch_instance

    def collection(self, _name):
        return _FakeCollection()


class CacheMetricsTests(unittest.TestCase):
    def test_writes_one_summary_per_drs_with_calculated_hit_rate(self):
        client = _FakeClient()
        metrics = {
            "drs-a": {
                "drsNo": "100", "consignmentsProcessed": 10,
                "invalidAddresses": 1, "cacheLookups": 8,
                "exactHits": 4, "fuzzyHits": 2, "misses": 1,
                "cacheErrors": 1, "apiCalls": 2, "apiFailures": 1,
            },
            "drs-b": {
                "drsNo": "200", "consignmentsProcessed": 5,
                "invalidAddresses": 0, "cacheLookups": 5,
                "exactHits": 0, "fuzzyHits": 0, "misses": 5,
                "cacheErrors": 0, "apiCalls": 5, "apiFailures": 0,
            },
        }

        with patch("app.db.cache_metrics.get_fs_client", return_value=client):
            write_drs_cache_metrics(metrics, datetime(2026, 9, 21, tzinfo=timezone.utc))

        self.assertTrue(client.batch_instance.committed)
        self.assertEqual(len(client.batch_instance.writes), 2)
        payloads = [payload for _document, payload in client.batch_instance.writes]
        self.assertEqual({payload["drsId"] for payload in payloads}, {"drs-a", "drs-b"})
        self.assertEqual(
            next(payload["hitRate"] for payload in payloads if payload["drsId"] == "drs-a"),
            75.0,
        )


if __name__ == "__main__":
    unittest.main()
