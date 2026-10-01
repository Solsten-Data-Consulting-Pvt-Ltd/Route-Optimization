"""save_to_cache must not let a later DRS-memo group steal an existing doc's tag.

Two memo groups can normalise to the same cache key ("Unit-17A, 18/2A ..." and
"Unit 17 ..." both become "ambalipura sarjapura road ..."). Before the fix the
later group overwrote `drs_memo_group`, so verifying the earlier group verified
nothing. Firestore is replaced by a tiny in-memory fake.
"""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("GOOGLE_MAPS_API_KEY", "test-key")
os.environ.setdefault("BQ_PROJECT", "test-project")
os.environ.setdefault("BQ_DATASET", "test_dataset")
os.environ.setdefault("BQ_TABLE", "test_table")
os.environ.setdefault("BQ_STRUCTURED_TABLE", "test_structured_table")
os.environ.setdefault("FIRESTORE_PROJECT", "test-project")

from google.cloud import firestore
from google.cloud.firestore_v1.transforms import ArrayUnion

from app.db import geocache


def _apply(doc, fields):
    for k, v in fields.items():
        if isinstance(v, ArrayUnion):
            cur = list(doc.get(k) or [])
            doc[k] = cur + [x for x in v.values if x not in cur]
        elif v is firestore.SERVER_TIMESTAMP:
            doc[k] = "ts"
        else:
            doc[k] = v


class FakeSnap:
    def __init__(self, store, doc_id):
        self.id, self._store = doc_id, store
        self.exists = doc_id in store.docs
        self.reference = FakeRef(store, doc_id)

    def to_dict(self):
        return dict(self._store.docs[self.id])


class FakeRef:
    def __init__(self, store, doc_id):
        self.store, self.id = store, doc_id

    def get(self):
        return FakeSnap(self.store, self.id)

    def set(self, fields):
        self.store.docs[self.id] = {}
        _apply(self.store.docs[self.id], fields)

    def update(self, fields):
        _apply(self.store.docs[self.id], fields)


class FakeQuery:
    def __init__(self, store, conds=()):
        self.store, self.conds = store, conds

    def where(self, field, op, value):
        return FakeQuery(self.store, self.conds + ((field, op, value),))

    def limit(self, _n):
        return self

    def stream(self):
        for doc_id, d in list(self.store.docs.items()):
            ok = True
            for field, op, value in self.conds:
                got = d.get(field)
                ok &= (got == value) if op == "==" else (value in (got or []))
            if ok:
                yield FakeSnap(self.store, doc_id)


class FakeCollection(FakeQuery):
    def __init__(self):
        super().__init__(self)
        self.docs = {}
        self.conds = ()

    def document(self, doc_id):
        return FakeRef(self, doc_id)

    def batch_update(self, group, fields):
        pass


class FakeBatch:
    def __init__(self):
        self.ops = []

    def update(self, ref, fields):
        self.ops.append((ref, fields))

    def commit(self):
        for ref, fields in self.ops:
            ref.update(fields)


RESULT = {"latitude": 12.92, "longitude": 77.67, "pincode": "560103", "place_id": "P1"}
GROUP_A, GROUP_B = "D1:aaaa", "D1:bbbb"
# Both normalise to "ambalipura sarjapura road bellandur gate bengaluru karnataka india".
ADDR_A = "Ambalipura, Sarjapur Road, Bellandur Gate, Bengaluru, Karnataka 560102, India"
ADDR_B = "Ambalipura, Sarjapur Road, Bellandur Gate, Bangalore, Karnataka 560102, India"


class GroupTagTests(unittest.TestCase):
    def setUp(self):
        self.col = FakeCollection()
        client = type("C", (), {"batch": staticmethod(FakeBatch)})
        self.patches = [
            patch.object(geocache, "_collection", lambda: self.col),
            patch.object(geocache, "get_fs_client", lambda: client),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def test_later_group_with_same_key_does_not_steal_the_tag(self):
        self.assertEqual(geocache.normalize_address(ADDR_A), geocache.normalize_address(ADDR_B))
        geocache.save_to_cache(ADDR_A, RESULT, lookup_address=ADDR_A, drs_memo_group=GROUP_A)
        geocache.save_to_cache(ADDR_B, RESULT, lookup_address=ADDR_B, drs_memo_group=GROUP_B)

        self.assertEqual(len(self.col.docs), 1)               # still one doc
        doc = next(iter(self.col.docs.values()))
        self.assertEqual(doc["drs_memo_group"], GROUP_A)       # creating group kept
        self.assertEqual(doc["drs_memo_groups"], [GROUP_A, GROUP_B])

    def test_verifying_either_group_verifies_the_doc(self):
        geocache.save_to_cache(ADDR_A, RESULT, lookup_address=ADDR_A, drs_memo_group=GROUP_A)
        geocache.save_to_cache(ADDR_B, RESULT, lookup_address=ADDR_B, drs_memo_group=GROUP_B)
        self.assertEqual(geocache.verify_cache_group(GROUP_A, "ops"), 1)   # was 0 before
        self.assertTrue(next(iter(self.col.docs.values()))["verified"])

    def test_second_group_reaches_the_doc_by_array_lookup(self):
        geocache.save_to_cache(ADDR_A, RESULT, lookup_address=ADDR_A, drs_memo_group=GROUP_A)
        geocache.save_to_cache(ADDR_B, RESULT, lookup_address=ADDR_B, drs_memo_group=GROUP_B)
        self.assertEqual(len(geocache._docs_by_group(GROUP_B)), 1)

    def test_legacy_doc_with_scalar_group_only_is_still_found(self):
        self.col.docs["old"] = {"drs_memo_group": GROUP_A, "verified": False}
        self.assertEqual(len(geocache._docs_by_group(GROUP_A)), 1)
        self.assertEqual(geocache.verify_cache_group(GROUP_A, "ops"), 1)

    def test_verify_commits_in_chunks_for_large_groups(self):
        for i in range(1100):
            self.col.docs[f"d{i}"] = {"drs_memo_group": GROUP_A, "verified": False}
        commits = []
        real_commit = FakeBatch.commit
        FakeBatch.commit = lambda self_: (commits.append(len(self_.ops)), real_commit(self_))
        try:
            self.assertEqual(geocache.verify_cache_group(GROUP_A, "ops"), 1100)
        finally:
            FakeBatch.commit = real_commit
        self.assertTrue(all(n <= 500 for n in commits))
        self.assertEqual(sum(commits), 1100)
        self.assertTrue(all(d["verified"] for d in self.col.docs.values()))


if __name__ == "__main__":
    unittest.main()
