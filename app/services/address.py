"""Address text helpers.

Mirrors the text handling in the hermes-save Cloud Function exactly: minimal
normalization only. The address structure is deliberately left intact, because
the Places text search resolves the raw, human-written address better than an
aggressively "cleaned" one.
"""

import re


def normalize_to_single_line(text: str) -> str:
    """Minimal normalization - preserve address structure."""
    if not text:
        return ""
    # Replace all newline/carriage-return/tab variants with a plain space.
    flattened = re.sub(r"[\r\n\t]+", " ", text)
    # Collapse any remaining multi-space runs (double spaces, etc.).
    single_line = re.sub(r"\s+", " ", flattened).strip()
    return single_line


def clean_address_for_geocoding(text: str) -> str:
    """MINIMAL cleaning - only handle whitespace and obvious issues.

    Do NOT destroy address structure. Kept for callers that want it; the save
    pipeline does not apply it (same as the Cloud Function).
    """
    if not text:
        return ""

    # Only clean whitespace and control characters
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = re.sub(r"\s+", " ", text).strip()

    # Remove duplicate commas but KEEP the structure
    text = re.sub(r",\s*,", ",", text)

    # No space before comma/semicolon, exactly one space after.
    text = re.sub(r"\s*,\s*", ", ", text)
    text = re.sub(r"\s*;\s*", "; ", text)

    # Clean up empty segments (but don't remove too aggressively)
    parts = [p.strip() for p in text.split(",")]
    parts = [p for p in parts if p and p.lower() not in ("", "n/a", "na")]
    text = ", ".join(parts)

    return text


def extract_pincode(address: str):
    """Extract 6-digit pincode from address."""
    if not address:
        return None
    match = re.search(r"\b\d{6}\b", address)
    return match.group(0) if match else None


def build_geocode_address(receiver_name: str, receiver_address: str) -> str:
    """Build the Places query: the receiver address only, structure intact.

    OCR now splits the human recipient name from the business/organization,
    and `receiver_name` is a person's name. A personal name does not match a
    place in Google Places and can skew or empty the result, so it is no
    longer prefixed. `receiver_name` stays in the signature for callers.
    """
    return str(receiver_address or "").strip()


# Unit-level tokens that often make Places return nothing ("Flat 3B, 2nd
# floor, Wing C, ..."). Each match runs to the next comma. `#` has no word
# boundary before it, hence the look-behind instead of \b.
UNIT_TOKENS = re.compile(
    r"(?:(?<![\w])(?:\d+(?:st|nd|rd|th)\s+)?(?:flat\s+no|flat|floor|flr|wing|room|door\s+no|house\s+no)\b|#)[^,]*,?",
    re.IGNORECASE,
)

RETRY_COMPONENT_KEYS = ("premise", "sub_locality", "locality", "city", "postal_code")


def build_retry_address(receiver_address, components=None):
    """Shortened query for ONE retry after Places returns zero results.

    Prefers the OCR address components 3PL stores on the consignment
    (receiver.addressComponent): premise, sub_locality/locality, city,
    postal_code. Without them, strips unit tokens (flat, floor, wing, room,
    door/house no, #...) from the address. Returns None when the result is
    empty or no different from the original, so no pointless retry is made.
    """
    if components:
        parts = [str(components.get(k) or "").strip() for k in RETRY_COMPONENT_KEYS]
        text = ", ".join(p for p in parts if p)
    else:
        text = UNIT_TOKENS.sub("", receiver_address or "")
    text = normalize_to_single_line(text)
    text = re.sub(r"\s*,\s*(,\s*)+", ", ", text).strip(" ,")
    original = normalize_to_single_line(receiver_address or "")
    return text if text and text != original else None


# --- Building-name retry (geocoding.places_search_classified) -------------
# Words that are unit labels, not part of a building's name.
_UNIT_WORDS = {
    "flat", "flats", "no", "door", "house", "floor", "flr", "wing", "block",
    "tower", "plot", "unit", "shop", "room", "apt", "level", "ground", "gf",
}


def building_name_from_components(components):
    """The building/complex name from the OCR address components, with flat,
    door, floor and block numbers and labels removed: "8, Manu Kurmel" ->
    "Manu Kurmel", "Flat 1002, Sai Avenue Block-2" -> "Sai Avenue". None when
    nothing name-like is left."""
    if not isinstance(components, dict):
        return None
    for key in ("premise", "sub_premise"):
        raw = str(components.get(key) or "")
        words = []
        for token in re.split(r"[\s,/#:.-]+", raw):
            if not token or any(ch.isdigit() for ch in token):
                continue
            if token.lower() in _UNIT_WORDS:
                continue
            words.append(token)
        name = " ".join(words).strip()
        if len(re.sub(r"[^A-Za-z]", "", name)) >= 3:
            return name
    return None


def build_building_query(components):
    """(query, building name) for the building-name retry, or None.
    Query = building, locality, postal code - e.g. "Manu Kurmel, Hadosiddapura,
    560035". Needs a building name and at least a locality or PIN code."""
    building = building_name_from_components(components)
    if not building:
        return None
    locality = str(components.get("locality") or components.get("sub_locality") or "").strip()
    pincode = str(components.get("postal_code") or "").strip()
    if not locality and not pincode:
        return None
    if locality and locality.lower() == building.lower():
        return None
    query = ", ".join(p for p in (building, locality, pincode) if p)
    return query, building
