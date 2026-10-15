"""Shared normalizer examples.

tests/data/normalizer_vectors.json is kept byte-for-byte identical to
functions/src/helpers/normalizerVectors.json in the 3PL-Management-Suite repo,
which runs the same examples against its JavaScript copy of the rules. A
change to normalize_address / cache_key that is not made on both sides fails
one of the two test suites, instead of silently giving the same address two
cache ids.

To change the rules: edit both implementations, regenerate the expected values
from THIS implementation, and copy the file to the other repo.
"""

import json
import os
import unittest
from pathlib import Path

os.environ.setdefault("GOOGLE_MAPS_API_KEY", "test-key")
os.environ.setdefault("BQ_PROJECT", "test-project")
os.environ.setdefault("BQ_DATASET", "test_dataset")
os.environ.setdefault("BQ_TABLE", "test_table")
os.environ.setdefault("BQ_STRUCTURED_TABLE", "test_structured_table")
os.environ.setdefault("FIRESTORE_PROJECT", "test-project")

from app.db.geocache import cache_key, normalize_address

VECTORS = json.loads(
    (Path(__file__).parent / "data" / "normalizer_vectors.json").read_text(encoding="utf-8")
)["vectors"]


class NormalizerVectorTests(unittest.TestCase):
    def test_every_shared_example_still_normalizes_the_same(self):
        self.assertGreater(len(VECTORS), 20)
        for vector in VECTORS:
            with self.subTest(input=vector["input"]):
                normalized = normalize_address(vector["input"])
                self.assertEqual(normalized, vector["normalized"])
                expected_key = cache_key(normalized) if normalized else None
                self.assertEqual(expected_key, vector["cache_key"])
