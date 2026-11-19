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


def _clean_name(raw):
    """A name-like string with flat/door/floor numbers and unit labels removed,
    or None when fewer than 3 letters are left."""
    words = []
    for token in re.split(r"[\s,/#:.-]+", str(raw or "")):
        if not token or any(ch.isdigit() for ch in token):
            continue
        if token.lower() in _UNIT_WORDS:
            continue
        words.append(token)
    name = " ".join(words).strip()
    return name if len(re.sub(r"[^A-Za-z]", "", name)) >= 3 else None


def building_name_from_components(components):
    """The building/complex name from the OCR address components, with flat,
    door, floor and block numbers and labels removed: "8, Manu Kurmel" ->
    "Manu Kurmel", "Flat 1002, Sai Avenue Block-2" -> "Sai Avenue". None when
    nothing name-like is left."""
    if not isinstance(components, dict):
        return None
    for key in ("premise", "sub_premise"):
        name = _clean_name(components.get(key))
        if name:
            return name
    return None


# Words that make a first address part an area or a road, not a building.
_AREA_WORDS = {
    "road", "rd", "main", "cross", "layout", "nagar", "colony", "street", "st",
    "lane", "circle", "junction", "post", "village", "stage", "sector",
    "extension", "extn", "avenue", "highway", "bridge", "gate",
}
_LANDMARK_LEADS = {"to", "near", "opp", "opposite", "behind", "beside", "next", "off", "adjacent", "nr"}
_AREA_KEYS = ("locality", "sub_locality", "route", "street", "neighborhood", "landmark")


def address_match_names(components, address=None):
    """Names the label gives the destination building, for judging Places
    results: premise and sub_premise (cleaned), plus every part of the address
    text that looks like a building or business of at least two words
    ("Prestige Ferns Residency", "Mataji Gas Agency") - not an area, road,
    the label's own locality/landmark, a landmark phrase (near, opposite...), or
    a single word. Only the first three parts are read. Both premise and
    parts are used because a tower name ("Jasper") and the compound name can
    sit in different parts. Empty list when there is no building name."""
    comps = components if isinstance(components, dict) else {}
    names, seen = [], set()

    def add(name):
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)

    for raw in (comps.get("premise"), comps.get("sub_premise")):
        add(_clean_name(raw))
    area_values = {str(comps.get(k) or "").strip().lower() for k in _AREA_KEYS}
    # Only the first three parts: landmarks ("Wipro Corporate Office", "RRS
    # Global School") come after the building, and a landmark must not be
    # taken as the destination.
    for part in str(address or "").split(",")[:3]:
        name = _clean_name(part)
        if not name or len(name.split()) < 2:
            continue
        words = {w.lower() for w in re.findall(r"[A-Za-z]+", name)}
        if (words & _AREA_WORDS or name.lower() in area_values
                or name.split()[0].lower() in _LANDMARK_LEADS):
            continue
        add(name)
    return names


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
