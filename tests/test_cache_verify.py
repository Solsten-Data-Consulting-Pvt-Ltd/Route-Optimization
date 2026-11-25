"""Repeat-evidence tests for the cache-owner verification endpoints."""

import tests  # noqa: F401 - installs environment and offline service stubs
import unittest
from unittest.mock import patch

from google.cloud.firestore_v1.transforms import ArrayUnion

from app.services import cache_verify


class _Snap:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data or {})


class _Ref:
    def __init__(self, docs, doc_id):
        self.docs, self.id = docs, doc_id
        self.deleted = False

    def get(self):
        return _Snap(self.docs.get(self.id))

    def update(self, fields):
        doc = self.docs.setdefault(self.id, {})
        for key, value in fields.items():
            if isinstance(value, ArrayUnion):
                doc[key] = list(dict.fromkeys((doc.get(key) or []) + list(value.values)))
            else:
                doc[key] = value

    def set(self, fields, merge=False):
        if not merge:
            self.docs[self.id] = dict(fields)
        else:
            self.docs.setdefault(self.id, {}).update(fields)

    def delete(self):
        self.deleted = True
        self.docs.pop(self.id, None)


class _Collection:
    def __init__(self, docs):
        self.docs = docs

    def document(self, doc_id):
        return _Ref(self.docs, doc_id)


class _Client:
    def __init__(self, docs):
        self._docs = docs

    def collection(self, name):
        return _Collection(self._docs.setdefault(name, {}))


def observation(drs_id, latitude=12.0, longitude=77.0, accuracy=20):
    return {
        "drs_id": drs_id,
        "latitude": latitude,
        "longitude": longitude,
        "accuracy": accuracy,
    }


class CacheVerificationTests(unittest.TestCase):
    def setUp(self):
        self.entry = {"latitude": 12.0, "longitude": 77.0, "verified": False}
        self.routing = {"drsId": "DRS-1", "pincode": "560001"}
        self.consignment = {"receiver": {"address": "12 Main Road, Bengaluru 560001"}}
        self.client = _Client({"geocode_cache": {"cache-1": self.entry}})
        self.resolve = patch.object(
            cache_verify, "_resolve_owner",
            side_effect=lambda _cid: ("cache-1", self.entry, self.routing, self.consignment),
        )
        self.resolve.start()
        self.addCleanup(self.resolve.stop)
        self.client_patch = patch.object(cache_verify, "get_fs_client", return_value=self.client)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.metadata = patch.object(cache_verify, "set_routing_cache_metadata")
        self.metadata.start()
        self.addCleanup(self.metadata.stop)
        self.group = patch.object(cache_verify, "verify_cache_group")
        self.group.start()
        self.addCleanup(self.group.stop)

    def test_one_delivery_within_500_m_verifies(self):
        # ~400 m north of the pin.
        result = cache_verify.confirm("C1", observation("ignored", 12.0036, 77.0), "driver-1")
        self.assertEqual((result["visitCount"], result["verified"]), (1, True))
        self.assertTrue(self.entry["verified"])

    def test_delivery_beyond_500_m_does_not_verify(self):
        # ~600 m north of the pin.
        result = cache_verify.confirm("C1", observation("ignored", 12.0054, 77.0), "driver-1")
        self.assertEqual((result["visitCount"], result["verified"]), (0, False))
        self.assertFalse(self.entry["verified"])

    def test_one_drs_still_counts_as_one_visit(self):
        cache_verify.confirm("C1", observation("ignored"), "driver-1")
        again = cache_verify.confirm("C2", observation("ignored"), "driver-1")
        self.assertEqual(again["visitCount"], 1)
        self.assertEqual(len(self.entry["observations"]), 1)

    def test_low_accuracy_observation_is_ignored(self):
        result = cache_verify.confirm("C1", observation("ignored", accuracy=101), "driver")
        self.assertEqual((result["visitCount"], result["verified"]), (0, False))
        self.assertFalse(result["observationAccepted"])
        self.assertNotIn("observations", self.entry)

    def test_three_agreeing_away_visits_suggest_a_pin(self):
        self.entry["observations"] = [
            observation("D1", 12.004, 77.0),
            observation("D2", 12.0041, 77.0),
            observation("D3", 12.0042, 77.0),
        ]
        suggestion = cache_verify.suggested_pin(self.entry)
        self.assertEqual(suggestion["visit_count"], 3)
        self.assertGreater(suggestion["latitude"], 12.003)

    def test_suggestion_stays_tight_while_verify_radius_is_500_m(self):
        self.assertEqual(cache_verify.GEOCODE_CACHE_VERIFY_RADIUS_METERS, 500)
        self.assertEqual(cache_verify.GEOCODE_CACHE_SUGGEST_RADIUS_METERS, 150)
        # Three visits ~300 m apart do not agree on one spot.
        self.entry["observations"] = [
            observation("D1", 12.004, 77.0),
            observation("D2", 12.0067, 77.0),
            observation("D3", 12.0094, 77.0),
        ]
        self.assertIsNone(cache_verify.suggested_pin(self.entry))

    def test_correct_returns_existing_cross_drs_visit_count(self):
        self.entry["observations"] = [observation("D1"), observation("D2")]
        result = cache_verify.correct("C1", 12.1, 77.1, "admin")
        self.assertEqual(result["visitCount"], 2)
        self.assertTrue(self.entry["verified"])
        self.assertEqual((self.entry["latitude"], self.entry["longitude"]), (12.1, 77.1))

    def test_restore_replaces_old_entry_and_deletes_new_one(self):
        previous = {"id": "cache-1", "created": False, "entry": {"latitude": 1.0}}
        result = cache_verify.restore(previous)
        self.assertEqual(result, {"id": "cache-1", "restored": True})
        self.assertEqual(self.client._docs["geocode_cache"]["cache-1"], {"latitude": 1.0})

        self.client._docs["geocode_cache"]["new-cache"] = {"latitude": 2.0}
        cache_verify.restore({"id": "new-cache", "created": True, "entry": {}})
        self.assertNotIn("new-cache", self.client._docs["geocode_cache"])

    def test_restore_accepts_a_pre_switchover_flattened_payload(self):
        self.entry["observations"] = [observation("D1")]
        cache_verify.restore({
            "id": "cache-1", "created": False,
            "latitude": 11.0, "longitude": 76.0, "verified": False,
            "source": "api", "verified_by": None,
        })
        self.assertEqual((self.entry["latitude"], self.entry["longitude"]), (11.0, 76.0))
        self.assertFalse(self.entry["verified"])
        self.assertIn("observations", self.entry)


if __name__ == "__main__":
    unittest.main()


class ForeignOwnerTests(unittest.TestCase):
    """A fuzzy hit once linked Purva Skydale to the RBD Still Water entry
    (prod 2026-11-25); a correction then overwrote RBD's pin."""

    RBD = "rbd still water apartments silver country road haralur road bengaluru karnataka india"
    SKYDALE_ADDR = "Tower, Purva Skydale Apartments, Harlur, Silver County Road, Bangalore, Karnataka 560068, India"

    def setUp(self):
        self.skydale = cache_verify.normalize_address(self.SKYDALE_ADDR)
        self.docs = {"geocode_cache": {"rbd": {
            "address_normalized": self.RBD,
            "address_normalized_variants": [self.RBD, self.skydale],
            "latitude": 12.8980267, "longitude": 77.6653239, "verified": True,
        }}}
        self.client = _Client(self.docs)
        self.consignment = {"receiver": {"address": self.SKYDALE_ADDR}}
        self.routing = {"drsId": "DRS-2", "geocode_cache_id": "rbd"}
        for target, value in (
            ("get_fs_client", self.client),
        ):
            p = patch.object(cache_verify, target, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        for name, value in (
            ("_routing", self.routing), ("_consignment", self.consignment),
        ):
            p = patch.object(cache_verify, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(cache_verify, "get_cache_entry",
                         side_effect=lambda cid: (cid, dict(self.docs["geocode_cache"][cid]))
                         if cid in self.docs["geocode_cache"] else None)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(cache_verify, "find_cache_entry",
                         side_effect=lambda n: next(
                             ((cid, dict(d)) for cid, d in self.docs["geocode_cache"].items()
                              if n in (d.get("address_normalized_variants") or [])), None))
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(cache_verify, "invalidate_snapshot")
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(cache_verify.drs_memo, "load_entries", return_value={})
        p.start()
        self.addCleanup(p.stop)

    def test_other_building_entry_is_not_the_owner_and_loses_the_stray_spelling(self):
        # ArrayRemove is not modelled by the fake _Ref, so check the call instead.
        removed = []
        real_update = _Ref.update

        def update(ref, fields):
            from google.cloud.firestore_v1.transforms import ArrayRemove
            value = fields.get("address_normalized_variants")
            if isinstance(value, ArrayRemove):
                removed.extend(value.values)
                return
            real_update(ref, fields)

        with patch.object(_Ref, "update", update):
            owner = cache_verify._resolve_owner("HRD62119876")
        self.assertIsNone(owner[0])
        self.assertEqual(set(removed), {self.skydale})
        self.assertEqual(self.docs["geocode_cache"]["rbd"]["latitude"], 12.8980267)

    def test_same_building_spelling_variant_is_still_the_owner(self):
        self.consignment["receiver"]["address"] = (
            "RBD Still Water Apartments, Silver County Road, Haralur Road, Bengaluru, Karnataka 560102"
        )
        owner = cache_verify._resolve_owner("RKT10770703")
        self.assertEqual(owner[0], "rbd")
