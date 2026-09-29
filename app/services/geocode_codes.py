"""Every geocode status, resolution, source and message this repo writes.

Route-Optimization is the only writer of the three routing fields:

  geocode_status      coarse state        success | needs_review | failed
  geocode_resolution  formal reason       KNOWN_GOOD, CONFIDENT, ... (below)
  geocode_source      where the pin came  confirmed, preview, memo, places, ...

The labels shown to ops, drivers and customers live in the 3PL catalog; this
repo writes only these codes plus one standard English message per
resolution in geocode_error. The 3PL apps read exactly these names, so do not
rename them.
"""

STATUS_SUCCESS, STATUS_NEEDS_REVIEW, STATUS_FAILED = "success", "needs_review", "failed"

# Category 1 / 2 — no one acts
KNOWN_GOOD, CONFIDENT = "KNOWN_GOOD", "CONFIDENT"
# Category 3 — executive must fix (and 4 for SERVICE_ERROR, shown as "No location")
ZERO_RESULTS, MISSING_ADDRESS, SERVICE_ERROR = "ZERO_RESULTS", "MISSING_ADDRESS", "SERVICE_ERROR"
# Category 4 — ops review; the pin is saved and routed
MULTI_CANDIDATE, PINCODE_MISMATCH = "MULTI_CANDIDATE", "PINCODE_MISMATCH"

RESOLUTIONS = {
    KNOWN_GOOD, CONFIDENT, ZERO_RESULTS, MISSING_ADDRESS, SERVICE_ERROR,
    MULTI_CANDIDATE, PINCODE_MISMATCH,
}
REVIEW_RESOLUTIONS = {MULTI_CANDIDATE, PINCODE_MISMATCH}
FAILED_RESOLUTIONS = {ZERO_RESULTS, MISSING_ADDRESS, SERVICE_ERROR}

# Existing ERR_* codes in app/services/geocoding.py -> formal resolution.
ERR_TO_RESOLUTION = {
    "ADDRESS_NOT_FOUND": ZERO_RESULTS,            # incl. "found but no coordinates"
    "MISSING_ADDRESS": MISSING_ADDRESS,
    "GEOCODING_SERVICE_UNREACHABLE": SERVICE_ERROR,
    "GEOCODING_API_ERROR": SERVICE_ERROR,
}

# The only strings written to the routing row's geocode_error (flag on).
MESSAGES = {
    ZERO_RESULTS: "Address not found on the map.",
    MISSING_ADDRESS: "No receiver address on this consignment.",
    SERVICE_ERROR: "Map lookup unavailable — retry or place the pin.",
    MULTI_CANDIDATE: "Several possible locations found.",
    PINCODE_MISMATCH: "Map result is in pincode {found}; address says {given}.",
}

SOURCE_CONFIRMED = "confirmed"
SOURCE_PREVIEW = "preview"
SOURCE_ADMIN = "admin"
SOURCE_MEMO_CORRECTED = "memo_corrected"
SOURCE_MEMO = "memo"
SOURCE_CACHE_EXACT = "cache_exact"
SOURCE_CACHE_FUZZY = "cache_fuzzy"
SOURCE_PLACES = "places"
SOURCE_PLACES_RETRY = "places_retry"

SOURCES = {
    SOURCE_CONFIRMED, SOURCE_PREVIEW, SOURCE_ADMIN, SOURCE_MEMO_CORRECTED,
    SOURCE_MEMO, SOURCE_CACHE_EXACT, SOURCE_CACHE_FUZZY, SOURCE_PLACES,
    SOURCE_PLACES_RETRY,
}


def resolution_for_error(error_code):
    """Formal resolution for an ERR_* code; unknown codes count as SERVICE_ERROR."""
    return ERR_TO_RESOLUTION.get(error_code, SERVICE_ERROR)


def status_for(result, resolution, use_v2):
    """geocode_status for a row. needs_review is only ever written with the flag on."""
    if result is None:
        return STATUS_FAILED
    if use_v2 and resolution in REVIEW_RESOLUTIONS:
        return STATUS_NEEDS_REVIEW
    return STATUS_SUCCESS


def message_for(resolution, found=None, given=None):
    """Standard geocode_error text for a resolution, or None (KNOWN_GOOD / CONFIDENT)."""
    template = MESSAGES.get(resolution)
    if template is None:
        return None
    return template.format(found=found or "unknown", given=given or "unknown")
