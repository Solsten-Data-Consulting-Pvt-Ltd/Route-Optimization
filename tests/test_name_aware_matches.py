"""Fewer false "Several matches": results are judged by the building name on
the label (real Google results from prod DRS DHRD10229-10237, 19 Nov 2026).

Only the old MULTI_CANDIDATE case (2+ in-pincode results more than 300 m apart)
looks at names. Same compound / duplicate listing / one clear match -> CONFIDENT;
none named like the label -> building-name retry, else unchanged (Several matches).
"""

import tests  # noqa: F401  env vars + offline feature flags (tests/__init__.py)

import unittest
from unittest import mock

from app.services import geocoding
from app.services.address import address_match_names, build_building_query
from app.services.geocode_codes import CONFIDENT, MULTI_CANDIDATE

PIN = "560035"


def place(pid, name, lat, lon, pin=PIN, types=("premise", "establishment")):
    return {"id": pid, "displayName": {"text": name}, "formattedAddress": f"{name}, Bengaluru {pin}",
            "types": list(types), "location": {"latitude": lat, "longitude": lon},
            "addressComponents": [{"types": ["postal_code"], "longText": pin, "shortText": pin}]}


def classify(places, names, pin=PIN):
    return geocoding.classify_places_ex(places, pin, True, names)


# SJR Palazza City (ANR80902908): entry, towers, clubhouse of ONE compound.
PALAZZA = [
    place("e", "SJR Palazza City Entry", 12.9100, 77.7000),
    place("c", "SJR Palazza City", 12.9120, 77.7010),
    place("f", "F- Block SJR Palazza City", 12.9121, 77.7012),
    place("j", "J-Block Sears, SJR Palazza City", 12.9128, 77.7015),
    place("cl", "SJR Palazza City Club House", 12.9139, 77.7030),
]
# Prestige Ferns Residency + a temple on the road named in the address (DPI1272164).
FERNS = [
    place("t", "Sri Dinne Prasanna Anjaneya Swamy Temple", 12.9050, 77.6500, types=("hindu_temple", "establishment")),
    place("f", "Prestige Ferns Residency", 12.9000, 77.6450),
]
# Landmark hijack: the label is Sunny Brooks, every result is the Wipro campus (CLT6118776).
WIPRO = [
    place("w1", "Wipro Limited, Sarjapur", 12.8420, 77.6850),
    place("w2", "Wipro Technologies", 12.8422, 77.6850),
    place("w3", "Wipro Tower S3, SJP2", 12.8438, 77.6860),
    place("w4", "Wipro - Kodathi", 12.8730, 77.7100),
]
# Two listings of one place 1.3 km apart (Amaatra Academy): stays a real choice.
AMAATRA = [place("a", "The Amaatra Academy", 12.9300, 77.6700), place("b", "Amaatra academy off Sarjapur road", 12.9420, 77.6700)]


class MatchNames(unittest.TestCase):
    def test_premise_and_building_like_address_parts(self):
        names = address_match_names({"premise": "Jasper Oion The Woods of East"},
                                    "Jasper, Oion The Woods of East, Chikkanayakahalli, Bengaluru 560035")
        self.assertIn("Jasper Oion The Woods of East", names)
        self.assertIn("Oion The Woods of East", names)

    def test_roads_areas_landmarks_and_single_words_are_skipped(self):
        names = address_match_names({"locality": "Doddakannelli"},
                                    "Doddakannelli, Sarjapur Main Road, Gear School Road, Wipro Corporate Office, Bengaluru")
        self.assertEqual(names, [])
        names = address_match_names({}, "Sunny Brooks, Kingfisher Lane, Sarjapur Road, Wipro Corporate Office")
        self.assertEqual(names, ["Sunny Brooks"])   # the Wipro landmark is the 4th part: never read

    def test_landmark_phrase_is_skipped(self):
        self.assertEqual(address_match_names({}, "Hadosiddapura, to Wipro corporate office, X"), [])


class Classify(unittest.TestCase):
    def test_same_compound_is_confident(self):
        p, res, _shown, state = classify(PALAZZA, ["SJR Palazza City"])
        self.assertEqual((res, state), (CONFIDENT, "compound"))
        self.assertIn("SJR Palazza City", p["displayName"]["text"])

    def test_one_clear_match_beats_noise(self):
        p, res, _shown, state = classify(FERNS, ["Zonash Elegance", "Prestige Ferns Residency"])
        self.assertEqual((res, state), (CONFIDENT, "one"))
        self.assertEqual(p["id"], "f")

    def test_landmark_hijack_has_no_name_match(self):
        _p, res, _shown, state = classify(WIPRO, ["Sunny Brooks"])
        self.assertEqual((res, state), (MULTI_CANDIDATE, "none"))

    def test_one_word_never_matches_a_longer_name(self):
        _p, _res, _shown, state = classify(WIPRO, ["Wipro Corporate"])   # 'corporate' missing from results
        self.assertEqual(state, "none")

    def test_far_apart_matches_stay_several_and_list_only_matches(self):
        noise = place("n", "Pasta Street", 12.9500, 77.6900)
        _p, res, shown, state = classify(AMAATRA + [noise], ["THE AMAATRA ACADEMY"])
        self.assertEqual((res, state), (MULTI_CANDIDATE, "far"))
        self.assertEqual({x["id"] for x in shown}, {"a", "b"})

    def test_area_level_result_is_never_a_name_match(self):
        area = place("ar", "Sunny Brooks", 12.90, 77.70, types=("sublocality", "political"))
        other = place("o", "Cafe Coffee Day", 12.95, 77.75)
        _p, _res, _shown, state = classify([area, other], ["Sunny Brooks"])
        self.assertEqual(state, "none")

    def test_no_building_name_keeps_todays_answer(self):
        p, res, shown, state = classify(PALAZZA, [])
        self.assertEqual((res, shown, state), (MULTI_CANDIDATE, None, None))
        self.assertEqual(p["id"], "e")

    def test_names_do_not_change_single_or_close_results(self):
        close = [place("a", "X One", 12.90, 77.60), place("b", "Y Two", 12.9001, 77.6001)]
        _p, res, _s, state = classify(close, ["Totally Different"])
        self.assertEqual((res, state), (CONFIDENT, None))

    def test_classify_places_is_unchanged(self):
        self.assertEqual(geocoding.classify_places(PALAZZA, PIN, True)[1], MULTI_CANDIDATE)


class EndToEnd(unittest.TestCase):
    ADDRESS = "Sunny Brooks, Kingfisher Lane, Sarjapur Road, Wipro Corporate Office, Bengaluru 560035"
    COMPONENTS = {"premise": "Sunny Brooks", "locality": "Doddakannelli", "postal_code": PIN}

    def run_search(self, first, second, components=COMPONENTS):
        calls = []

        def fake(query, max_retries, page_size=None):
            calls.append(query)
            return (first if len(calls) == 1 else second), None, None

        with mock.patch.object(geocoding, "_search_places", side_effect=fake):
            out = geocoding.places_search_classified(
                self.ADDRESS, use_v2=True, building_query=build_building_query(components),
                match_names=address_match_names(components, self.ADDRESS))
        return out, calls

    def test_no_name_match_then_retry_finds_the_building(self):
        sunny = place("s", "Sunny Brooks", 12.8500, 77.6900)
        (result, _r, _c, resolution, source), calls = self.run_search(WIPRO, [sunny])
        self.assertEqual(len(calls), 2)
        self.assertEqual((resolution, source), (CONFIDENT, "places_retry"))
        self.assertNotIn("_no_name_match", result)

    def test_no_name_match_and_retry_finds_nothing_keeps_several_matches(self):
        (result, reason, code, resolution, source), calls = self.run_search(WIPRO, WIPRO)
        self.assertEqual(len(calls), 2)
        self.assertIsNotNone(result)                       # today's pin is kept
        self.assertEqual((resolution, source, code), (MULTI_CANDIDATE, "places", None))
        self.assertEqual(len(result["candidates"]), 4)     # today's list
        self.assertNotIn("_no_name_match", result)

    def test_no_name_match_without_building_query_keeps_several_matches(self):
        with mock.patch.object(geocoding, "_search_places", return_value=(WIPRO, None, None)):
            result, _r, _c, resolution, source = geocoding.places_search_classified(
                self.ADDRESS, use_v2=True, match_names=["Sunny Brooks"])
        self.assertEqual((resolution, source), (MULTI_CANDIDATE, "places"))
        self.assertNotIn("_no_name_match", result)

    def test_same_compound_needs_no_retry(self):
        comps = {"premise": "SJR Palazza City", "postal_code": PIN}
        (result, _r, _c, resolution, source), calls = self.run_search(PALAZZA, [], comps)
        self.assertEqual((resolution, source, len(calls)), (CONFIDENT, "places", 1))
        self.assertNotIn("_no_name_match", result)

    def test_without_match_names_behaviour_is_unchanged(self):
        with mock.patch.object(geocoding, "_search_places", return_value=(WIPRO, None, None)):
            result, _r, _c, resolution, source = geocoding.places_search_classified(self.ADDRESS, use_v2=True)
        self.assertEqual((resolution, source), (MULTI_CANDIDATE, "places"))


if __name__ == "__main__":
    unittest.main()
