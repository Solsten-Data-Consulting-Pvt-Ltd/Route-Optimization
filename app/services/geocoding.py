"""Geocoding via the Google Places API v1 text search.

`places_search_address` is behaviourally identical to the hermes-save Cloud
Function: one text query per consignment, places[0], no cache.
`places_search_classified` adds the address-resolution classification (and,
behind geocodeResolutionV2, up to 5 candidates plus one shortened retry).
"""

import logging
import re
import time

import requests
from rapidfuzz import fuzz

from app.config import (
    COMPOUND_SPREAD_M,
    GOOGLE_MAPS_API_KEY,
    PLACES_CANDIDATE_PAGE_SIZE,
    PLACES_SEARCH_URL,
    SAME_PLACE_SPREAD_M,
)
from app.services.address import extract_pincode
from app.services.geocode_codes import (
    CONFIDENT,
    MISSING_ADDRESS,
    MULTI_CANDIDATE,
    PINCODE_MISMATCH,
    SOURCE_PLACES,
    SOURCE_PLACES_RETRY,
    ZERO_RESULTS,
    resolution_for_error,
)

logger = logging.getLogger(__name__)

ERR_MISSING_ADDRESS = "MISSING_ADDRESS"
ERR_ADDRESS_NOT_FOUND = "ADDRESS_NOT_FOUND"
ERR_SERVICE_UNREACHABLE = "GEOCODING_SERVICE_UNREACHABLE"
ERR_API_ERROR = "GEOCODING_API_ERROR"
ERR_NOT_FOUND_IN_FIRESTORE = "CONSIGNMENT_NOT_FOUND"

FIELD_MASK = (
    "places.id,"
    "places.displayName,"
    "places.formattedAddress,"
    "places.location,"
    "places.addressComponents,"
    "places.types"
)

COMMERCIAL_TYPES = {
    "establishment",
    "point_of_interest",
    "store",
    "shopping_mall",
    "hospital",
    "restaurant",
    "lodging",
    "school",
    "bank",
}


def _get_places_component(components, component_type, short_name: bool = False):
    for component in components:
        types = component.get("types", [])

        if component_type in types:
            if short_name:
                return component.get("shortText")

            return component.get("longText")

    return None


def _search_places(address: str, max_retries: int, page_size=None):
    """One Places text search with HTTP retries.

    Returns (places, error_reason, error_code); `places` is a list (possibly
    empty) on success, None on a service failure.
    """
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
        "X-Goog-FieldMask": FIELD_MASK,
    }

    payload = {
        "textQuery": address,
        "regionCode": "IN",
    }
    if page_size:
        payload["pageSize"] = page_size

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(
                PLACES_SEARCH_URL,
                headers=headers,
                json=payload,
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

        except requests.RequestException as e:
            logger.error(
                "Places API HTTP error for '%s' attempt %d/%d: %s",
                address,
                attempt,
                max_retries,
                e,
            )

            if attempt < max_retries:
                time.sleep(2 ** attempt)
                continue

            return (
                None,
                "Places service unreachable. Please try again.",
                ERR_SERVICE_UNREACHABLE,
            )

        return data.get("places", []) or [], None, None

    return None, "Address lookup failed after multiple attempts.", ERR_SERVICE_UNREACHABLE


def _place_pincode(place):
    return _get_places_component(place.get("addressComponents", []), "postal_code")


def _build_result(place, address, address_pincode):
    location = place.get("location", {})
    components = place.get("addressComponents", [])

    pincode_result = _get_places_component(components, "postal_code")
    locality = _get_places_component(components, "locality")
    area = _get_places_component(components, "sublocality")
    street_number = _get_places_component(components, "street_number")
    route_name = _get_places_component(components, "route")
    district = _get_places_component(components, "administrative_area_level_2")
    state = _get_places_component(components, "administrative_area_level_1")
    country_code = _get_places_component(components, "country", short_name=True)

    pincode_match = True

    if address_pincode and pincode_result and address_pincode != pincode_result:
        pincode_match = False

        logger.warning(
            "Pincode mismatch for %s: Original=%s, Places=%s",
            address[:50],
            address_pincode,
            pincode_result,
        )

    return {
        "latitude": location.get("latitude"),
        "longitude": location.get("longitude"),
        "pincode": pincode_result,
        "locality": locality,
        "area": area,
        "location_type": None,
        "partial_match": False,
        "types": place.get("types", []),
        "formatted_address": place.get("formattedAddress"),
        "place_id": place.get("id"),
        "street_number": street_number,
        "route_name": route_name,
        "district": district,
        "state": state,
        "country_code": country_code,
        "pincode_match": pincode_match,
    }


def _legacy_resolution(place, address_pincode):
    found = _place_pincode(place)
    if address_pincode and found and address_pincode != found:
        return PINCODE_MISMATCH
    return CONFIDENT


def _max_spread_m(places):
    # Existing haversine (km) from the TSP module; imported here so this module
    # does not pull in OR-Tools at import time.
    from app.services.tsp import haversine_distance

    points = [
        (p.get("location", {}).get("latitude"), p.get("location", {}).get("longitude"))
        for p in places
    ]
    spread = 0.0
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            spread = max(spread, haversine_distance(*points[i], *points[j]) * 1000.0)
    return spread


def label_name_score(label_name, place_name):
    """0-100: how much a Places name looks like the building name on the label.
    building_name_similarity (spelling tolerant) OR a word-level match: at
    least two meaningful words of the label name, and at least 75% of them,
    each appear (spelling tolerant) in the place's name - "Bluebell Klassik" ~
    "Klassik Landmark Bluebell Tower", "Jasper Oion The Woods of East" ~
    "Odion The Woods of East Phase 4". One shared word is never enough
    ("Wipro" must not match "Wipro Technologies")."""
    score = building_name_similarity(label_name, place_name)
    label_words = _name_key(label_name).split()
    place_words = _name_key(place_name).split()
    if len(label_words) >= 2 and place_words:
        hits = sum(
            1 for w in label_words
            if max(fuzz.ratio(w, p) for p in place_words) >= 80
        )
        if hits >= 2 and hits / len(label_words) >= 0.75:
            score = max(score, 90)
    return score


def _name_matches(in_area, match_names):
    """[(score, place)] for in-pincode results (not areas/roads) whose name
    resembles a name on the label (similarity >= BUILDING_NAME_MIN_SIMILARITY)."""
    out = []
    for place in in_area:
        if is_area_level(place):
            continue
        name = (place.get("displayName") or {}).get("text")
        score = max((label_name_score(n, name) for n in match_names), default=0)
        if score >= BUILDING_NAME_MIN_SIMILARITY:
            out.append((score, place))
    return out


def _best_of_compound(matches):
    """Highest name score; ties go to the result nearest the group's centre."""
    from app.services.tsp import haversine_distance

    lats = [p["location"]["latitude"] for _s, p in matches]
    lons = [p["location"]["longitude"] for _s, p in matches]
    clat, clon = sum(lats) / len(lats), sum(lons) / len(lons)
    return max(
        matches,
        key=lambda m: (m[0], -haversine_distance(
            clat, clon, m[1]["location"]["latitude"], m[1]["location"]["longitude"])),
    )[1]


def classify_places_ex(places, address_pincode, use_v2, match_names=None):
    """classify_places plus name matching. Returns
    (place, resolution, shown_places, name_state).

    Only the old "several results far apart" case (MULTI_CANDIDATE) looks at
    names, when `match_names` (names on the label) is given:
      "one"      exactly one result is named like the label -> CONFIDENT
      "compound" several are, all within COMPOUND_SPREAD_M (towers, phases,
                 gates, duplicate listings) -> CONFIDENT, best-named one
      "far"      several, farther apart -> MULTI_CANDIDATE listing ONLY them
      "none"     none is named like the label (a landmark or random places);
                 the caller retries by building name, else keeps MULTI_CANDIDATE
    name_state is None when names were not used; shown_places is None unless
    the candidate list should be limited.
    """
    if not places:
        return None, ZERO_RESULTS, None, None
    if not use_v2:
        return places[0], _legacy_resolution(places[0], address_pincode), None, None

    located = [p for p in places if p.get("location")]
    if not located:
        return places[0], ZERO_RESULTS, None, None   # "found but no coordinates"
    in_area = (
        [p for p in located if _place_pincode(p) == address_pincode]
        if address_pincode else located
    )
    if not in_area:
        return located[0], PINCODE_MISMATCH, None, None
    if len(in_area) == 1 or _max_spread_m(in_area) <= SAME_PLACE_SPREAD_M:
        return in_area[0], CONFIDENT, None, None
    if not match_names:
        return in_area[0], MULTI_CANDIDATE, None, None

    matches = _name_matches(in_area, match_names)
    if not matches:
        return in_area[0], MULTI_CANDIDATE, None, "none"
    if len(matches) == 1:
        return matches[0][1], CONFIDENT, None, "one"
    matched_places = [p for _s, p in sorted(matches, key=lambda m: -m[0])]
    best = _best_of_compound(matches)
    if _max_spread_m(matched_places) <= COMPOUND_SPREAD_M:
        return best, CONFIDENT, None, "compound"
    return best, MULTI_CANDIDATE, matched_places, "far"


def classify_places(places, address_pincode, use_v2):
    """Pick a candidate and classify it. Returns (place, resolution).

    Flag off: today's pick (places[0]) with CONFIDENT / PINCODE_MISMATCH.
    Flag on:  prefer candidates in the address's pincode; several of them more
              than SAME_PLACE_SPREAD_M apart -> MULTI_CANDIDATE (first one kept).
    Name matching (classify_places_ex) is opt-in and not used here.
    """
    place, resolution, _shown, _state = classify_places_ex(places, address_pincode, use_v2)
    return place, resolution


MAX_STORED_CANDIDATES = 5


def _candidate_summary(place, address_pincode):
    """Compact, storable view of one Places candidate (what admin is shown)."""
    location = place.get("location") or {}
    pincode = _place_pincode(place)
    return {
        "name": (place.get("displayName") or {}).get("text"),
        "formatted_address": place.get("formattedAddress"),
        "latitude": location.get("latitude"),
        "longitude": location.get("longitude"),
        "pincode": pincode,
        "place_id": place.get("id"),
        "in_pincode": bool(address_pincode and pincode == address_pincode),
    }


def _candidate_summaries(places, address_pincode):
    return [
        _candidate_summary(p, address_pincode)
        for p in places[:MAX_STORED_CANDIDATES]
        if (p.get("location") or {}).get("latitude") is not None
        and (p.get("location") or {}).get("longitude") is not None
    ]


def _search_and_classify(query, address_pincode, use_v2, max_retries, match_names=None):
    """(result, error_reason, error_code, resolution) for one Places query.

    With use_v2, the returned result also carries `candidates`: every
    located place Google offered for this query (the set classify_places
    chose from). It's never cached — geocache/drs_memo copy only
    CACHED_RESULT_FIELDS — and is only stored on the routing doc for
    doubtful pins (save.py)."""
    places, error_reason, error_code = _search_places(
        query, max_retries, page_size=PLACES_CANDIDATE_PAGE_SIZE if use_v2 else None,
    )
    if places is None:
        return None, error_reason, error_code, resolution_for_error(error_code)

    if not places:
        return (
            None,
            "Address could not be found. Please check and correct the address.",
            ERR_ADDRESS_NOT_FOUND,
            ZERO_RESULTS,
        )

    place, resolution, shown, name_state = classify_places_ex(
        places, address_pincode, use_v2, match_names)

    if not place.get("location"):
        return (
            None,
            "Place was found but no coordinates were returned.",
            ERR_ADDRESS_NOT_FOUND,
            ZERO_RESULTS,
        )

    result = _build_result(place, query, address_pincode)
    if use_v2:
        result["candidates"] = _candidate_summaries(shown or places, address_pincode)
        if name_state == "none":
            result["_no_name_match"] = True     # popped by places_search_classified
    return result, None, None, resolution


# --- Building-name retry ---------------------------------------------------
# Google often answers an address whose building name is misspelt (handwritten
# "Mana Karmel" read as "Manu Kurmel" - SMG9041167, DHRD20055) with just the
# area ("Carmelaram, Hadosiddapura"), ~700 m off. When the answer is only an
# area or a road and the label has a building name, search once more with
# "building, locality, PIN" and take a candidate whose name is clearly the
# same building in the same PIN code. Nothing changes on screen: the source
# stays "places_retry" ("from Google"), and with no clear match the first
# answer is kept as before.

# Place types that mean "an area or a road", not a building or business.
AREA_LEVEL_TYPES = {
    "locality", "sublocality", "sublocality_level_1", "sublocality_level_2",
    "sublocality_level_3", "neighborhood", "political", "postal_code",
    "administrative_area_level_1", "administrative_area_level_2",
    "administrative_area_level_3", "colloquial_area", "route", "intersection",
    "geocode", "country",
}
BUILDING_NAME_MIN_SIMILARITY = 75
BUILDING_MAX_DISTANCE_M = 3000

_GENERIC_NAME_WORDS = {"the", "of", "apartment", "apartments", "apts", "apt",
                       "residency", "enclave", "layout", "society", "homes"}


def is_area_level(place):
    types = set(place.get("types") or [])
    return bool(types) and types <= AREA_LEVEL_TYPES


# Place types that mean "the centre of a pincode, city or state": Google found
# no street, area or building, so the pin is a point in the middle of a region
# and says nothing about where the parcel goes. (Roads and neighbourhoods are
# NOT in this set; they are coarse but a real label can name only a road.)
COARSE_TYPES = {
    "postal_code", "locality", "administrative_area_level_1",
    "administrative_area_level_2", "administrative_area_level_3",
    "country", "political",
}
COARSE_ADDRESS_REASON = (
    "Address has only a pincode or city, no street or area. "
    "Add the street or area, or drop the pin at the delivery point."
)


def is_coarse_level(place):
    """True when every type of the result is a pincode / city / state / country."""
    types = set(place.get("types") or [])
    return bool(types) and types <= COARSE_TYPES


def _name_key(text):
    words = re.findall(r"[a-z0-9]+", str(text or "").lower())
    return " ".join(w for w in words if w not in _GENERIC_NAME_WORDS)


def building_name_similarity(a, b):
    """0-100. Spelling-tolerant ("Manu Kurmel" ~ "Mana Karmel") and ignores
    generic words ("the", "of", "apartments") and spacing ("Century21")."""
    ka, kb = _name_key(a), _name_key(b)
    if not ka or not kb:
        return 0
    return max(fuzz.token_sort_ratio(ka, kb),
               fuzz.ratio(ka.replace(" ", ""), kb.replace(" ", "")))


def pick_building_candidate(places, building, address_pincode, near):
    """The best candidate that is clearly `building`: a building/business (not
    an area), name similarity >= BUILDING_NAME_MIN_SIMILARITY, in the address's
    PIN code when both are known, and within BUILDING_MAX_DISTANCE_M of the
    first answer `near` (lat, lon). None when no candidate qualifies."""
    from app.services.tsp import haversine_distance

    best, best_score = None, 0
    for place in places or []:
        loc = place.get("location") or {}
        if loc.get("latitude") is None or loc.get("longitude") is None:
            continue
        if is_area_level(place):
            continue
        pin = _place_pincode(place)
        if address_pincode and pin and pin != address_pincode:
            continue
        if near and near[0] is not None and haversine_distance(
                near[0], near[1], loc["latitude"], loc["longitude"]) * 1000.0 > BUILDING_MAX_DISTANCE_M:
            continue
        score = building_name_similarity(building, (place.get("displayName") or {}).get("text"))
        if score >= BUILDING_NAME_MIN_SIMILARITY and score > best_score:
            best, best_score = place, score
    return best


def _building_retry(result, building_query, address_pincode, max_retries):
    """Re-resolve an area-level `result` by building name. Returns the new
    result dict, or None to keep `result`."""
    if not building_query or result is None:
        return None
    query, building = building_query
    places, _reason, _code = _search_places(query, max_retries, page_size=PLACES_CANDIDATE_PAGE_SIZE)
    if not places:
        return None
    place = pick_building_candidate(
        places, building, address_pincode or extract_pincode(query),
        (result.get("latitude"), result.get("longitude")),
    )
    if place is None:
        logger.info("Building retry for '%s': no clear match; keeping the area answer", building)
        return None
    logger.info("Building retry: '%s' resolved to '%s' instead of area '%s'", building,
                (place.get("displayName") or {}).get("text"), result.get("formatted_address"))
    new = _build_result(place, query, address_pincode)
    if "candidates" in result:
        new["candidates"] = _candidate_summaries(places, address_pincode)
    return new


def _coarse_failure(address):
    """Pincode/city-only answer: treated like "address not found" so no pin is
    saved, the stop reads "No match" and blocks SOD until a pin is placed."""
    logger.info("Coarse (pincode/city-level) result for '%s'; treating as not found",
                address[:60])
    return None, COARSE_ADDRESS_REASON, ERR_ADDRESS_NOT_FOUND, ZERO_RESULTS, None


def places_search_classified(address: str, use_v2: bool = False,
                             retry_address=None, max_retries: int = 3,
                             building_query=None, match_names=None):
    """Resolve one address and classify the result.

    Returns (result, error_reason, error_code, resolution, source), source
    being "places", "places_retry" or None on failure.

    With `use_v2`, a ZERO_RESULTS answer is retried ONCE with `retry_address`
    (see address.build_retry_address) before giving up; the retry result is
    classified the same way.
    """
    if not address or not address.strip():
        return (None, "No address provided for this consignment.", ERR_MISSING_ADDRESS,
                MISSING_ADDRESS, None)

    pincode = extract_pincode(address)

    result, error_reason, error_code, resolution = _search_and_classify(
        address, pincode, use_v2, max_retries, match_names=match_names,
    )
    if result is not None and result.pop("_no_name_match", False):
        # Several results, none named like the label (a landmark, or random
        # places around an area name): try the building by name. With no
        # clear hit the answer stays exactly as before (Several matches).
        if building_query:
            try:
                better = _building_retry(result, building_query, pincode, max_retries)
            except Exception:
                logger.exception("Building retry failed for '%s'", address[:60])
                better = None
            if better is not None:
                return better, None, None, CONFIDENT, SOURCE_PLACES_RETRY
        logger.info("No result named like the label for '%s'; keeping Several matches", address[:60])
        return result, None, None, resolution, SOURCE_PLACES
    if result is not None:
        # Area/road-only answer and the label names a building: try the
        # building by name (see _building_retry). Never fails the geocode.
        if use_v2 and building_query and set(result.get("types") or []) and \
                set(result.get("types") or []) <= AREA_LEVEL_TYPES:
            try:
                better = _building_retry(result, building_query, pincode, max_retries)
            except Exception:
                logger.exception("Building retry failed for '%s'", address[:60])
                better = None
            if better is not None:
                return better, None, None, CONFIDENT, SOURCE_PLACES_RETRY
        if use_v2 and is_coarse_level(result):
            return _coarse_failure(address)
        return result, None, None, resolution, SOURCE_PLACES

    if use_v2 and error_code == ERR_ADDRESS_NOT_FOUND and retry_address:
        logger.info("Places zero results for '%s'; retrying with '%s'",
                    address[:60], retry_address[:60])
        r_result, _r_reason, _r_code, r_resolution = _search_and_classify(
            retry_address, pincode or extract_pincode(retry_address), use_v2, max_retries,
        )
        if r_result is not None:
            if use_v2 and is_coarse_level(r_result):
                return _coarse_failure(address)
            return r_result, None, None, r_resolution, SOURCE_PLACES_RETRY

    return None, error_reason, error_code, resolution, None


def places_search_address(address: str, max_retries: int = 3):
    """Resolve one address. Returns (result, error_reason, error_code).

    Unchanged contract for existing callers: today's single query and
    places[0] pick. Use places_search_classified() for the resolution.
    """
    result, error_reason, error_code, _resolution, _source = places_search_classified(
        address, use_v2=False, max_retries=max_retries,
    )
    return result, error_reason, error_code


def derive_exception_flag(place_result: dict):
    if place_result.get("pincode_match") is False:
        return "PINCODE_MISMATCH"

    return None


def derive_is_commercial(place_result: dict):
    types = set(place_result.get("types", []))
    return bool(types.intersection(COMMERCIAL_TYPES))
