"""Area-only Places answers are re-resolved by building name.

SMG9041167 (dev DHRD20055, 19 Nov): handwritten "Mana Karmel" was read as
"Manu Kurmel"; Places answered "Carmelaram, Hadosiddapura" (an area) ~700 m
from the building and it was saved as CONFIDENT. Searching "Manu Kurmel,
Hadosiddapura, 560035" finds "Mana Karmel"; it is taken because the name is
clearly the same building in the same PIN code.
"""

import tests  # noqa: F401  env vars + offline feature flags (tests/__init__.py)

import unittest
from unittest import mock

from app.services import geocoding
from app.services.address import build_building_query, building_name_from_components

AREA = {"id": "area", "displayName": {"text": "Carmelaram"}, "types": ["sublocality", "political"],
        "formattedAddress": "Carmelaram, Hadosiddapura, Chikkakannalli, Karnataka 560035",
        "location": {"latitude": 12.9020038, "longitude": 77.7058073},
        "addressComponents": [{"types": ["postal_code"], "longText": "560035", "shortText": "560035"}]}
MANA = {"id": "mana", "displayName": {"text": "Mana Karmel"}, "types": ["premise", "establishment"],
        "formattedAddress": "Mana Karmel, Hadosiddapura, Bengaluru, Karnataka 560035",
        "location": {"latitude": 12.8956866, "longitude": 77.70577},
        "addressComponents": [{"types": ["postal_code"], "longText": "560035", "shortText": "560035"}]}
OTHER = dict(MANA, id="other", displayName={"text": "Prestige Lakeside Habitat"})
FAR_PIN = dict(MANA, id="far", addressComponents=[{"types": ["postal_code"], "longText": "560103",
                                                   "shortText": "560103"}])

COMPONENTS = {"sub_premise": "8, Manu Kurmel", "premise": "", "locality": "Hadosiddapura",
              "postal_code": "560035"}
ADDRESS = "Manu Kurmel, Hadosiddapura, Sarjapur Rd, RGA Tech Park, Bangalore, Karnataka 560035, India"


def run(first, second, components=COMPONENTS, use_v2=True):
    calls = []

    def fake_search(query, max_retries, page_size=None):
        calls.append(query)
        return (first if len(calls) == 1 else second), None, None

    with mock.patch.object(geocoding, "_search_places", side_effect=fake_search):
        out = geocoding.places_search_classified(
            ADDRESS, use_v2=use_v2, building_query=build_building_query(components))
    return out, calls


class BuildingName(unittest.TestCase):
    def test_strips_flat_numbers_and_labels(self):
        self.assertEqual(building_name_from_components({"sub_premise": "8, Manu Kurmel"}), "Manu Kurmel")
        self.assertEqual(building_name_from_components({"sub_premise": "Flat 1002, Sai Avenue Block-2"}),
                         "Sai Avenue")
        self.assertEqual(building_name_from_components({"premise": "Plot No 234, Green Valley Society"}),
                         "Green Valley Society")
        self.assertIsNone(building_name_from_components({"sub_premise": "Flat No 17"}))

    def test_query_is_building_locality_pin(self):
        self.assertEqual(build_building_query(COMPONENTS),
                         ("Manu Kurmel, Hadosiddapura, 560035", "Manu Kurmel"))
        self.assertIsNone(build_building_query({"sub_premise": "Flat 3", "locality": "X"}))
        self.assertIsNone(build_building_query(None))

    def test_similarity_tolerates_misreads_and_generic_words(self):
        self.assertGreaterEqual(geocoding.building_name_similarity("Manu Kurmel", "Mana Karmel"), 75)
        self.assertGreaterEqual(geocoding.building_name_similarity(
            "ODION THE WOODS OF EAST", "Odion Woods of East"), 90)
        self.assertGreaterEqual(geocoding.building_name_similarity(
            "Century21 Waterfront Apartment", "Century 21 Waterfront"), 90)
        self.assertLess(geocoding.building_name_similarity("Manu Kurmel", "Prestige Lakeside Habitat"), 50)


class BuildingRetry(unittest.TestCase):
    def test_area_answer_is_replaced_by_the_named_building(self):
        (result, _r, _c, resolution, source), calls = run([AREA], [OTHER, MANA])
        self.assertEqual(calls[1], "Manu Kurmel, Hadosiddapura, 560035")
        self.assertEqual((result["latitude"], result["longitude"]), (12.8956866, 77.70577))
        self.assertEqual((resolution, source), ("CONFIDENT", "places_retry"))

    def test_no_clear_match_keeps_the_first_answer(self):
        (result, _r, _c, _res, source), _ = run([AREA], [OTHER])
        self.assertEqual(result["place_id"], "area")
        self.assertEqual(source, "places")

    def test_other_pin_code_or_far_away_is_not_taken(self):
        (result, *_), _ = run([AREA], [FAR_PIN])
        self.assertEqual(result["place_id"], "area")
        far = dict(MANA, location={"latitude": 12.99, "longitude": 77.75})
        (result, *_), _ = run([AREA], [far])
        self.assertEqual(result["place_id"], "area")

    def test_building_answer_is_not_searched_again(self):
        (result, *_), calls = run([MANA], [OTHER])
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["place_id"], "mana")

    def test_no_building_name_no_extra_search(self):
        (result, *_), calls = run([AREA], [MANA], components={"locality": "Hadosiddapura"})
        self.assertEqual(len(calls), 1)

    def test_resolution_v2_off_is_unchanged(self):
        (result, *_), calls = run([AREA], [MANA], use_v2=False)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["place_id"], "area")

    def test_retry_error_keeps_the_first_answer(self):
        with mock.patch.object(geocoding, "_building_retry", side_effect=RuntimeError("x")), \
             mock.patch.object(geocoding, "_search_places", return_value=([AREA], None, None)):
            result, *_ = geocoding.places_search_classified(
                ADDRESS, use_v2=True, building_query=build_building_query(COMPONENTS))
        self.assertEqual(result["place_id"], "area")


if __name__ == "__main__":
    unittest.main()
