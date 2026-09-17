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
    """Build address for geocoding WITHOUT destroying structure.

    Google's API handles various formats well, so the receiver name is simply
    prefixed as a business name when it is not already part of the address.
    """
    name = str(receiver_name or "").strip()
    addr = str(receiver_address or "").strip()

    if not addr and not name:
        return ""

    # If both exist and name is NOT already in address, add it as business name
    if name and addr:
        # Check if name is already in address (case insensitive)
        if name.lower() in addr.lower():
            return addr
        return f"{name}, {addr}"

    return addr or name
