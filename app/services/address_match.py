"""Same-place matching for consignments inside one DRS.

Used only by the DRS address memo (`app/db/drs_memo.py`). Nothing here is
sent to Places or used as the global `geocode_cache` key; it only decides
whether two consignments in the SAME DRS go to the same place, so the second
one can reuse the first one's pin instead of a fresh Places call.

Inputs come from the consignment document:

  receiver.fullAddress           raw label text (preferred for matching)
  receiver.address               OCR-formatted address (fallback)
  receiver.phone                 receiver's phone - the ONLY phone source used
  receiver.addressComponent      postal_code, premise, sub_premise, locality, city

Rules, in order (see `drs_match`):

  1. Different pincode                      -> never the same place.
  2. Same receiver phone                    -> same place, unless both
                                               addresses carry door numbers
                                               and they differ.
  3. Either address is road-level           -> no text match (e.g. OCR text
     (no door number, < 3 distinctive words)   "Kodathi Village Main Road,
                                               Kodathi Gate, Bangalore ..."
                                               is shared by the whole road).
  4. Door numbers must be identical, then   -> exact key, or one address's
                                               words contained in the other's,
                                               or token_sort_ratio >= cutoff.
"""

import hashlib
import re
from typing import Iterable, Optional

from rapidfuzz import fuzz

from app.config import (
    DRS_MEMO_CONTAINMENT_RATIO,
    DRS_MEMO_FUZZY_THRESHOLD,
    DRS_MEMO_MIN_CONTAINED_TOKENS,
)
from app.services.address import normalize_to_single_line

PINCODE_RE = re.compile(r"\b\d{6}\b")
# Indian mobile, optionally prefixed with +91 / 91. Only STRIPPED from the
# address text so it cannot affect text comparison; never used as a phone.
MOBILE_IN_TEXT_RE = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)")
DOOR_NUMBER_RE = re.compile(r"\d+(?:/\d+)*[a-z]?")
ORDINAL_RE = re.compile(r"\d+(?:st|nd|rd|th)")

CITY_VARIANTS = {
    "bangalore": "bengaluru",
    "banglore": "bengaluru",
    "bengalore": "bengaluru",
    "bangaluru": "bengaluru",
    "blr": "bengaluru",
    "sarjapur": "sarjapura",
    "amblipura": "ambalipura",
}

# Spelling fixes and filler words ("" = drop the token).
WORD_VARIANTS = {
    "flore": "floor",
    "flr": "floor",
    "opp": "opposite",
    "rd": "road",
    "no": "",
    "pincode": "",
    "pin": "",
    "near": "",
    "nr": "",
}

# Words that say nothing about WHICH building on a road.
GENERIC_WORDS = {
    "road", "main", "cross", "street", "village", "gate", "layout", "nagar",
    "stage", "phase", "sector", "block", "floor", "opposite", "behind",
    "bengaluru", "karnataka", "india", "district", "taluk", "post",
}


def match_key(address: str) -> str:
    """Comparison key: lowercase, no punctuation, no phone/pincode, spellings
    standardised, repeated words dropped. Keeps '140/1' intact."""
    if not address:
        return ""
    s = address.lower()
    s = MOBILE_IN_TEXT_RE.sub(" ", s)
    s = PINCODE_RE.sub(" ", s)
    s = re.sub(r"[^a-z0-9/]+", " ", s)
    out = []
    for token in s.split():
        token = CITY_VARIANTS.get(token, token)
        token = WORD_VARIANTS.get(token, token)
        token = token.strip("/")
        if token and token not in out:
            out.append(token)
    return " ".join(out)


def door_numbers(key: str) -> set:
    """Door / plot / shop numbers in a match key: '31', '140/1', '12a'.
    Ordinals like '2nd' or '1st' (floors, stages) are ignored."""
    return {
        t for t in key.split()
        if DOOR_NUMBER_RE.fullmatch(t) and not ORDINAL_RE.fullmatch(t)
    }


def is_precise(key: str, locality_words: Iterable[str] = ()) -> bool:
    """False for road-level addresses that many receivers could share."""
    if door_numbers(key):
        return True
    distinctive = set(key.split()) - GENERIC_WORDS - set(locality_words or ())
    return len(distinctive) >= 3


def clean_phone(value) -> Optional[str]:
    digits = re.sub(r"\D", "", str(value or ""))[-10:]
    return digits if len(digits) == 10 and digits[0] in "6789" else None


def extract_pincode(*texts: str) -> Optional[str]:
    for text in texts:
        m = PINCODE_RE.search(text or "")
        if m:
            return m.group(0)
    return None


def _entry_id(key: str) -> str:
    return hashlib.sha256(f"drs-v1|{key}".encode("utf-8")).hexdigest()[:32]


def consignment_match_info(receiver: dict) -> dict:
    """Everything the DRS memo needs from `consignment.receiver`."""
    receiver = receiver or {}
    comp = receiver.get("addressComponent") or {}

    formatted = normalize_to_single_line(str(receiver.get("address") or ""))
    raw = normalize_to_single_line(str(receiver.get("fullAddress") or ""))
    match_address = raw or formatted

    # Receiver's phone only (string or list) - never numbers from the text.
    phone_field = receiver.get("phone")
    if not isinstance(phone_field, (list, tuple)):
        phone_field = [phone_field]
    phones = sorted({p for p in map(clean_phone, phone_field) if p})

    postal = str(comp.get("postal_code") or "").strip()
    pincode = postal if PINCODE_RE.fullmatch(postal) else extract_pincode(raw, formatted)

    extra_numbers = " ".join(
        str(comp.get(k) or "") for k in ("premise", "sub_premise")
    )
    key = match_key(f"{match_address} {extra_numbers}")
    locality_words = match_key(
        f"{comp.get('locality') or ''} {comp.get('city') or ''}"
    ).split()

    return {
        "match_address": match_address,
        "match_key": key,
        "entry_id": _entry_id(key) if key else None,
        "phones": phones,
        "pincode": pincode,
        "locality_words": locality_words,
    }


def match_info_from_address(address: str) -> dict:
    """Fallback when only an address string is available (no consignment doc)."""
    key = match_key(address)
    return {
        "match_address": address or "",
        "match_key": key,
        "entry_id": _entry_id(key) if key else None,
        "phones": [],
        "pincode": extract_pincode(address),
        "locality_words": [],
    }


def _text_match(a_key: str, b_key: str) -> Optional[str]:
    if a_key == b_key:
        return "exact"
    ta, tb = set(a_key.split()), set(b_key.split())
    small = ta if len(ta) <= len(tb) else tb
    if len(small) >= DRS_MEMO_MIN_CONTAINED_TOKENS and \
            len(ta & tb) / len(small) >= DRS_MEMO_CONTAINMENT_RATIO:
        return "contained"
    if fuzz.token_sort_ratio(a_key, b_key) >= DRS_MEMO_FUZZY_THRESHOLD:
        return "fuzzy"
    return None


def drs_match(new: dict, entry: dict) -> Optional[str]:
    """Return 'phone' | 'exact' | 'contained' | 'fuzzy' if `new` and `entry`
    are the same place, else None. Both are match-info dicts."""
    a_key, b_key = new.get("match_key") or "", entry.get("match_key") or ""
    if not a_key or not b_key:
        return None

    pa, pb = new.get("pincode"), entry.get("pincode")
    if pa and pb and pa != pb:
        return None

    na, nb = door_numbers(a_key), door_numbers(b_key)

    if set(new.get("phones") or ()) & set(entry.get("phones") or ()):
        if na and nb and na != nb:
            return None          # same person, different building (shop vs home)
        return "phone"

    if not (is_precise(a_key, new.get("locality_words"))
            and is_precise(b_key, entry.get("locality_words"))):
        return None

    if na != nb:
        return None

    return _text_match(a_key, b_key)
