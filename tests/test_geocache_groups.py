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

from google.api_core.exceptions import AlreadyExists
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
        elif v is firestore.DELETE_FIELD:
            doc.pop(k, None)
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

    def create(self, fields):
        # Real Firestore create(): refuses when the id already exists.
        if self.id in self.store.docs:
            raise AlreadyExists("document already exists")
        self.set(fields)

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


class UnverifiedExpiryTests(unittest.TestCase):
    """Unverified docs carry `expire_at` (Firestore TTL); verified docs never do."""

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

    def test_new_unverified_doc_gets_expire_at_three_days_out(self):
        from datetime import datetime, timedelta, timezone
        addr = "77 Park Avenue, Bengaluru 560002"
        geocache.save_to_cache(addr, RESULT, lookup_address=addr)
        doc = next(iter(self.col.docs.values()))
        delta = doc["expire_at"] - datetime.now(timezone.utc)
        self.assertTrue(timedelta(days=2, hours=23) < delta <= timedelta(days=3))

    def test_rewriting_an_unverified_doc_pushes_expire_at_out(self):
        addr = "77 Park Avenue, Bengaluru 560002"
        geocache.save_to_cache(addr, RESULT, lookup_address=addr)
        doc = next(iter(self.col.docs.values()))
        from datetime import datetime, timedelta, timezone
        doc["expire_at"] = datetime.now(timezone.utc) - timedelta(days=1)  # nearly gone
        geocache.save_to_cache(addr, RESULT, lookup_address=addr)
        self.assertEqual(len(self.col.docs), 1)               # still no duplicate
        self.assertGreater(doc["expire_at"], datetime.now(timezone.utc) + timedelta(days=2))

    def test_writing_to_a_verified_doc_never_adds_expire_at(self):
        addr = "12 MG Road, Bengaluru 560001"
        doc_id = geocache.cache_key(geocache.normalize_address(addr))
        self.col.docs[doc_id] = {"verified": True, "hit_count": 3}
        geocache.save_to_cache(addr, RESULT, lookup_address=addr)
        self.assertNotIn("expire_at", self.col.docs[doc_id])

    def test_verifying_removes_expire_at(self):
        addr = "77 Park Avenue, Bengaluru 560002"
        geocache.save_to_cache(addr, RESULT, lookup_address=addr, drs_memo_group=GROUP_A)
        self.assertIn("expire_at", next(iter(self.col.docs.values())))
        geocache.verify_cache_group(GROUP_A, "exec1")
        doc = next(iter(self.col.docs.values()))
        self.assertTrue(doc["verified"])
        self.assertNotIn("expire_at", doc)


if __name__ == "__main__":
    unittest.main()


class CreateOnlyTests(unittest.TestCase):
    """Saving must never overwrite or double an entry that is already there."""

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

    def test_a_doc_written_in_the_gap_is_refreshed_not_overwritten(self):
        # Another request creates the same id after our exists() check but
        # before our create(): simulate by making the first get() report "missing".
        addr = "12 MG Road, Bengaluru 560001"
        doc_id = geocache.cache_key(geocache.normalize_address(addr))
        self.col.docs[doc_id] = {"verified": True, "hit_count": 7, "latitude": 1.0}

        real_get = FakeRef.get
        calls = {"n": 0}

        def missing_once(ref):
            calls["n"] += 1
            snap = real_get(ref)
            if calls["n"] == 1:
                snap.exists = False
            return snap

        with patch.object(FakeRef, "get", missing_once), \
             patch.object(geocache, "_docs_by_group", lambda g: []):
            geocache.save_to_cache(addr, RESULT, lookup_address=addr)

        doc = self.col.docs[doc_id]
        self.assertEqual(len(self.col.docs), 1)
        self.assertTrue(doc["verified"])              # not reset
        self.assertEqual(doc["hit_count"], 7)         # not reset
        self.assertEqual(doc["latitude"], RESULT["latitude"])  # payload refreshed

    def test_an_entry_with_the_same_text_under_another_id_is_updated_not_doubled(self):
        addr = "12 MG Road, Bengaluru 560001"
        normalized = geocache.normalize_address(addr)
        self.col.docs["legacyAutoId123"] = {
            "address_normalized": normalized, "verified": True, "hit_count": 4,
        }
        geocache.save_to_cache(addr, RESULT, lookup_address=addr)

        self.assertEqual(list(self.col.docs), ["legacyAutoId123"])   # no second doc
        self.assertTrue(self.col.docs["legacyAutoId123"]["verified"])
        self.assertEqual(self.col.docs["legacyAutoId123"]["hit_count"], 4)
        self.assertEqual(self.col.docs["legacyAutoId123"]["latitude"], RESULT["latitude"])

    def test_a_brand_new_address_still_creates_one_unverified_doc(self):
        addr = "77 Park Avenue, Bengaluru 560002"
        geocache.save_to_cache(addr, RESULT, lookup_address=addr)
        self.assertEqual(len(self.col.docs), 1)
        doc = next(iter(self.col.docs.values()))
        self.assertFalse(doc["verified"])
        self.assertEqual(doc["hit_count"], 0)

