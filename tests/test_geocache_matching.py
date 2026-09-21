"""Tests for geocache normalize_address() and fuzzy matching logic.

No Firestore / Google API credentials required — only the pure-Python
normalize_address() function and the rapidfuzz scorer are exercised here.
Includes testing Fix 1 (address-only lookup key), Fix 2 (Plus Codes), and Fix 3 (building variants).
"""

import hashlib
import re
import unittest

from rapidfuzz import fuzz, utils

from app.services.address import build_geocode_address, normalize_to_single_line

# --- Mirrors app/db/geocache.py ---
GEOCODE_CACHE_FUZZY_THRESHOLD = 85

CITY_VARIANTS = {
    "bangalore": "bengaluru",
    "sarjapur":  "sarjapura",
    "amblipura": "ambalipura",
}

BUILDING_VARIANTS = {
    "r k complex": "rk com",
    "domasandra":  "dommasandra",
}

PINCODE_RE = re.compile(r"\b\d{6}\b")
PLUS_CODE_RE = re.compile(
    r"\b[23456789CFGHJMPQRVWX]{4,8}\+[23456789CFGHJMPQRVWX]{2,3}\b",
    re.IGNORECASE,
)


def normalize_address(address: str) -> str:
    if not address:
        return ""
    s = address.lower().strip()
    s = re.sub(r"[,\s]+", " ", s)
    s = PINCODE_RE.sub("", s)
    s = PLUS_CODE_RE.sub("", s)
    for variant, canonical in CITY_VARIANTS.items():
        s = re.sub(r"\b" + re.escape(variant) + r"\b", canonical, s)
    for verbose, compact in BUILDING_VARIANTS.items():
        s = s.replace(verbose, compact)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def cache_key(normalized: str) -> str:
    return hashlib.sha256(f"v1|{normalized}".encode("utf-8")).hexdigest()[:40]
# --- end copy ---


def make_lookup_str(name: str, address: str) -> str:
    """Mirror save.py Fix 1: lookup key is receiver_address (fallback to geocode string)."""
    n = normalize_to_single_line(name)
    a = normalize_to_single_line(address)
    return a if a else build_geocode_address(n, a)


def fuzzy_score(incoming_normalized: str, cached_normalized: str) -> float:
    return fuzz.token_sort_ratio(
        incoming_normalized,
        cached_normalized,
        processor=utils.default_process,
    )


CACHED_NAME    = "Nandakumar Narayanabhat"
CACHED_ADDRESS = (
    "Lalitha Nilaya, Nandu home, Narayana Nagar 2nd Block, "
    "5th Main Road, Bengaluru, Karnataka 560062"
)

CACHED_LOOKUP_STR  = make_lookup_str(CACHED_NAME, CACHED_ADDRESS)
CACHED_NORMALIZED  = normalize_address(CACHED_LOOKUP_STR)


class TestNormalizeAddress(unittest.TestCase):
    """Unit tests for normalize_address() including Fix 2 and Fix 3."""

    def test_pincode_stripped(self):
        result = normalize_address("Bengaluru, Karnataka 560062")
        self.assertNotIn("560062", result)

    def test_plus_code_stripped(self):
        result = normalize_address("M/S. NIBE MOTORS - VPMX+F5R, Dommasandra, Bengaluru")
        self.assertNotIn("vpmx+f5r", result)

    def test_building_variant_replaced(self):
        result = normalize_address("R K Complex, Domasandra, Sarjapur Main Road")
        self.assertIn("rk com", result)
        self.assertIn("dommasandra", result)

    def test_bangalore_variant_replaced(self):
        result = normalize_address("5th Main Road, Bangalore, Karnataka")
        self.assertIn("bengaluru", result)
        self.assertNotIn("bangalore", result)

    def test_sarjapur_variant_replaced(self):
        result = normalize_address("Sarjapur Road, Bangalore")
        self.assertIn("sarjapura", result)

    def test_amblipura_variant_replaced(self):
        result = normalize_address("Amblipura, Bangalore")
        self.assertIn("ambalipura", result)

    def test_lowercase_and_collapse(self):
        result = normalize_address("  MG Road,  ,  Bengaluru  ")
        self.assertEqual(result, "mg road bengaluru")

    def test_empty_returns_empty(self):
        self.assertEqual(normalize_address(""), "")
        self.assertEqual(normalize_address(None), "")


class TestCacheKey(unittest.TestCase):
    def test_same_input_same_key(self):
        self.assertEqual(cache_key(CACHED_NORMALIZED), cache_key(CACHED_NORMALIZED))

    def test_different_input_different_key(self):
        self.assertNotEqual(cache_key(CACHED_NORMALIZED), cache_key("some other address"))

    def test_key_is_40_chars(self):
        self.assertEqual(len(cache_key(CACHED_NORMALIZED)), 40)


class TestFix1RecipientIndependentMatching(unittest.TestCase):
    """Test that Fix 1 lets deliveries to the same building for different people share cache."""

    def test_same_building_different_person_is_exact_match(self):
        # Two employees at Shahi Exports:
        lookup_1 = make_lookup_str(
            "Mr. Ayyappa / Muneer, Shahi Exports Pvt. Ltd.",
            "Ambalipura, Bellandur, Sarjapur Road, Bangalore, Karnataka 560102, India",
        )
        lookup_2 = make_lookup_str(
            "Mr. Sareesh, Shahi Exports Pvt Ltd",
            "Ambalipura, Bellandur, Sarjapur Road, Bengaluru, Karnataka 560102, India",
        )
        norm_1 = normalize_address(lookup_1)
        norm_2 = normalize_address(lookup_2)
        score = fuzzy_score(norm_1, norm_2)
        self.assertEqual(score, 100.0)


class TestFuzzyMatchVariations(unittest.TestCase):
    def _run(self, name, address):
        lookup_str = make_lookup_str(name, address)
        normalized = normalize_address(lookup_str)
        score      = fuzzy_score(normalized, CACHED_NORMALIZED)
        hit        = score >= GEOCODE_CACHE_FUZZY_THRESHOLD
        print(
            f"\n  name={name!r}\n"
            f"  addr={address!r}\n"
            f"  lookup_str -> {lookup_str!r}\n"
            f"  normalized -> {normalized!r}\n"
            f"  score={score:.1f}  threshold={GEOCODE_CACHE_FUZZY_THRESHOLD}  "
            f"result={'HIT [PASS]' if hit else 'MISS [FAIL]'}"
        )
        return lookup_str, normalized, score, hit

    def test_different_recipient_same_address_is_100_hit(self):
        # Fix 1: If someone else receives parcel at Lalitha Nilaya
        _, _, score, hit = self._run(
            name="Suresh Bhat",
            address=CACHED_ADDRESS,
        )
        self.assertTrue(hit)
        self.assertEqual(score, 100.0)

    def test_variation2_bangalore_spelling_is_hit(self):
        _, _, score, hit = self._run(
            name="Nandakumar N",
            address=(
                "Lalitha Nilaya, Nandu home, Narayana Nagar 2nd Block, "
                "5th Main Road, Bangalore, Karnataka 560062"
            ),
        )
        self.assertTrue(hit)
        self.assertEqual(score, 100.0)

    def test_variation3_karnataka_missing_is_hit(self):
        _, _, score, hit = self._run(
            name="Nandakumar Narayanabhat",
            address=(
                "Lalitha Nilaya, Nandu Home, Narayana Nagar 2nd Block, "
                "5th Main Road, Bengaluru 560062"
            ),
        )
        self.assertTrue(hit)
        self.assertGreaterEqual(score, GEOCODE_CACHE_FUZZY_THRESHOLD)

    def test_completely_different_address_is_miss(self):
        _, _, score, hit = self._run(
            name="Ravi Kumar",
            address="42 Brigade Road, Shivajinagar, Bengaluru 560001",
        )
        self.assertFalse(hit)
        self.assertLess(score, GEOCODE_CACHE_FUZZY_THRESHOLD)


if __name__ == "__main__":
    unittest.main(verbosity=2)
