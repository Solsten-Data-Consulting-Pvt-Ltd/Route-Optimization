"""Geocoding via the Google Places API v1 text search.

Behaviourally identical to `places_search_address` in the hermes-save Cloud
Function: one text query per consignment, no candidate fallbacks, no cache.
"""

import logging
import time

import requests

from app.config import GOOGLE_MAPS_API_KEY, PLACES_SEARCH_URL
from app.services.address import extract_pincode

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


def places_search_address(address: str, max_retries: int = 3):
    """Resolve one address. Returns (result, error_reason, error_code)."""
    if not address or not address.strip():
        return None, "No address provided for this consignment.", ERR_MISSING_ADDRESS

    pincode = extract_pincode(address)

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
        "X-Goog-FieldMask": FIELD_MASK,
    }

    payload = {
        "textQuery": address,
        "regionCode": "IN",
    }

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

        places = data.get("places", [])

        if not places:
            return (
                None,
                "Address could not be found. Please check and correct the address.",
                ERR_ADDRESS_NOT_FOUND,
            )

        place = places[0]

        location = place.get("location", {})
        components = place.get("addressComponents", [])

        if not location:
            return (
                None,
                "Place was found but no coordinates were returned.",
                ERR_ADDRESS_NOT_FOUND,
            )

        pincode_result = _get_places_component(components, "postal_code")
        locality = _get_places_component(components, "locality")
        area = _get_places_component(components, "sublocality")
        street_number = _get_places_component(components, "street_number")
        route_name = _get_places_component(components, "route")
        district = _get_places_component(components, "administrative_area_level_2")
        state = _get_places_component(components, "administrative_area_level_1")
        country_code = _get_places_component(components, "country", short_name=True)

        pincode_match = True

        if pincode and pincode_result and pincode != pincode_result:
            pincode_match = False

            logger.warning(
                "Pincode mismatch for %s: Original=%s, Places=%s",
                address[:50],
                pincode,
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
        }, None, None

    return None, "Address lookup failed after multiple attempts.", ERR_SERVICE_UNREACHABLE


def derive_exception_flag(place_result: dict):
    if place_result.get("pincode_match") is False:
        return "PINCODE_MISMATCH"

    return None


def derive_is_commercial(place_result: dict):
    types = set(place_result.get("types", []))
    return bool(types.intersection(COMMERCIAL_TYPES))
