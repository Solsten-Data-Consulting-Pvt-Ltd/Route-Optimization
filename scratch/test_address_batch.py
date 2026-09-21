# scratch/test_address_batch.py
# Run with: python scratch/test_address_batch.py
# Tests the updated logic with Fix 1, Fix 2, and Fix 3.

import hashlib
import re
import sys

from rapidfuzz import fuzz, process, utils

# ── Updated from app/db/geocache.py ──────────────────────────────────────────
THRESHOLD = 85

CITY_VARIANTS = {
    "bangalore": "bengaluru",
    "sarjapur":  "sarjapura",
    "amblipura": "ambalipura",
}

# Fix 3 — building-name abbreviations
BUILDING_VARIANTS = {
    "r k complex": "rk com",
    "domasandra":  "dommasandra",
}

PINCODE_RE = re.compile(r"\b\d{6}\b")

# Fix 2 — Google Plus Codes
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
    s = PLUS_CODE_RE.sub("", s)  # Fix 2
    for variant, canonical in CITY_VARIANTS.items():
        s = re.sub(r"\b" + re.escape(variant) + r"\b", canonical, s)
    for verbose, compact in BUILDING_VARIANTS.items():  # Fix 3
        s = s.replace(verbose, compact)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def cache_key(normalized: str) -> str:
    return hashlib.sha256(f"v1|{normalized}".encode()).hexdigest()[:40]
# ─────────────────────────────────────────────────────────────────────────────


# ── From app/services/address.py ─────────────────────────────────────────────
def normalize_to_single_line(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[\r\n\t]+", " ", text)).strip()


def build_geocode_address(name: str, address: str) -> str:
    name = str(name or "").strip()
    addr = str(address or "").strip()
    if not addr and not name:
        return ""
    if name and addr:
        if name.lower() in addr.lower():
            return addr
        return f"{name}, {addr}"
    return addr or name
# ─────────────────────────────────────────────────────────────────────────────


RAW = """
1. Magmatic NDT Systems Private Limited - Lal Bahadur Shastry Nagar, 10th Block, Anjanapura Extension, 4th Cross Road, 2nd Main Road, Bangalore, Karnataka 560108, India
2. Matharaj, Trackon Couriers Pvt Ltd, Jayalakshmi Enterprises - Gubbalala 80 ft Road, Bangalore, Karnataka 560061, India
3. DEEPA BASANTANI - JRC Palladio, Phase 2, Bangalore, Karnataka 562125, India
4. RishiTha - VSR Premium Ladies Pb, Ambalipura, Sarjapur Road, Bengaluru, Karnataka 560035, India
5. P.Chanukyasri Reddy, B.Sc Physics, rd year, Azim Premji University - Sarjapura, Bikkanahalli Main Road, Bengaluru, Karnataka 562125, India
6. Mareeswaran K, Greaves Electric Mobility Pvt Ltd. - Konakunte Village, Velachenahalli, Bengaluru, Karnataka 560062, India
7. House of Soni - Inspira Winds of Life, KTM Layout, Sarjapur Road, Bangalore, Karnataka 562125, India
8. - - RK Com, Sarjapura Main Road, Bangalore, Karnataka 562125, India
9. Deepak Thakur - MS Layout, Jaraganahalli, Kanakapura Main Road, Bangalore, Karnataka 560078, India
10. RGRID CORPORATION OF INDIA LIMITED - Anahalli Village & Post, KM Kanakapura Road, Galore, Tamil Nadu 635117, India
11. Michael Kishore Bosco - Prestige City, Meridian Park, Tower 9, Yamare, Sarjapur, Marathahalli Road, Bangalore, Karnataka 562125, India
12. M/S. NIBE MOTORS - NO. 193/2, KATHA NO. 12, Sarjapur Road, Dommasandra, Bengaluru, Karnataka 562125, India
13. Rekha Kamalraj - Six Villa, Chikkadunasandra, Bangalore, Karnataka 562125, India
14. Sasi Kumar, Autoedge Advanced Automation Technologies Pvt Ltd - Dommasandra, Vemana Road, Sarjapur - Marathahalli Road, Karnataka 562125, India
15. Sasi Kumar, Autoedge Advanced Automation Technologies Pvt Ltd - Dommasandra, Vennila Road, Sarjapur Marathahalli Road, Karnataka 562125, India
16. Angel Care Pharma - Angel Care Women & Children Hospital, Vamare Village, Sarjapura Main Road, Bangalore, Karnataka 562125, India
17. PRATHAM MOTORS PVT LTD - DOMMASANDRA, SARJAPUR MAI, BENGALURU, KARNATAKA 562125, India
18. PRATHAM MOTORS PVT LTD - DOMMASANDRA, SARJAPUR MAIN ROAD, BENGALURU, KARNATAKA 562125, India
19. PRATHAM MOTORS PVT LTD - Yamare, Sarjapur Main Road, Bengaluru, Karnataka 562125, India
20. Pratham Motors Pvt Ltd - Dommasandra, Sarjapur Main Road, Bengaluru, Karnataka 562125, India
21. PRATHAM MOTORS PVT LTD - YAMARE, SARJAPUR MAIN ROAD, BENGALURU, KARNATAKA 562125, India
22. M/S. NIBE MOTORS - NO. 193/2, KATHA NO. 12, Sarjapur Road, Dommasandra, Bengaluru, Karnataka 562125, India
23. Load Controls India Private Limited - K. Ashwath Industrial Layout, Uttari Road, Karnataka 560082, India
24. Mr. Ayyappa / Muneer, Shahi Exports Pvt. Ltd. - Ambalipura, Bellandur, Sarjapur Road, Bangalore, Karnataka 560102, India
25. Mr. Sareesh, Shahi Exports Pvt Ltd - Ambalipura, Bellandur, Sarjapur Road, Bengaluru, Karnataka 560102, India
26. Sareesh, Shahi Exports Pvt. Ltd. - Ambalipura, Bellandur, Sarjapura Road, Bengaluru, Karnataka 560102, India
27. M/S. NIBE MOTORS - VPMX+F5R, Dommasandra, Bengaluru, Thigala Chowdadenahalli, Karnataka 562125
28. Twachaa Pharma - RK Com, Sarjapura Main Road, Bangalore, Karnataka 562125, India
29. Twachaa Pharmacy - R K Complex, Domasandra, Sarjapur Main Road, Bangalore, Karnataka 562125, India
30. Lakshmisree B - Sattva Signet, Kasavanahalli, Sarjapur Road, Bangalore, Karnataka 560035, India
31. Mr. Ayyappa/Muneer, Shahi Exports Pvt. Ltd. - Ambalipura, Sarjapur Road, Bangalore, Karnataka 560102, India
32. Mr. Sareesh, Shahi Exports Pvt. Ltd. - Ambalipura, Sarjapur Road, Bangalore, Karnataka 560102, India
33. Mr. Sipra Mondal / Nikunj Talekar, Shahi Exports Pvt. Ltd. - Ambalipura, Sarjapur Road, Bangalore, Karnataka 560102, India
34. Ayyappa/Muneer, Shahi Exports Pvt. Ltd. - Ambalipura, Sarjapur Road, Bangalore, Karnataka 560102, India
35. Mr. Gopal Prasad Sharma, C/O M. Subramani - Konankuntte Cross, Kanakpura Road, Bangalore, India
36. Prof Kalidasu Jakirathai Subramani G - Shanthari Anjaneyaswari Temple, Konanakunte, New Bank Colony, Bangalore, Karnataka 560062, India
37. Shyma - Konan Kunte, 4th Cross New Bank Colony Main Road, Bangalore South, Karnataka 560062, India
38. MEDIBGND ENTERPRISES PVT LTD - 1st Floor, No. 65, 3/670, Thimmaiah Compound, Near Old Post Office, Kanakapura Main Road, Doddakallasandra, Bengaluru, Karnataka 560062, India
39. Nagaraj S Birur - Rathnashree, Royal Park Residency, JP Nagar 9th Phase, Bengaluru, Karnataka 560062, India
40. Chatra - Narayana Nagar 1st Block, 11th Main, Doddakalsandra Bangalore, Karnataka 560062, India
41. MAMATHA NATARAY N - 4th Main Road, Nagegowdanapalya, Banashankari 6th Stage, Thalaghattapura, Bengaluru, Karnataka 560109, India
""".strip()


entries = []
for line in RAW.splitlines():
    line = line.strip()
    if not line:
        continue
    m = re.match(r"^\d+\.\s+(.*?)\s+-\s+(.+)$", line)
    if not m:
        continue

    raw_name = m.group(1).strip().lstrip("-").strip()
    raw_addr = m.group(2).strip()

    name = normalize_to_single_line(raw_name)
    addr = normalize_to_single_line(raw_addr)
    geocode_str = build_geocode_address(name, addr)
    
    # Fix 1: Cache lookup key is receiver_address (addr) rather than name + addr
    lookup_str = addr if addr else geocode_str
    normalized = normalize_address(lookup_str)

    entries.append({
        "idx":         line.split(".")[0],
        "raw_name":    raw_name,
        "raw_addr":    raw_addr,
        "geocode_str": geocode_str,
        "lookup_str":  lookup_str,
        "normalized":  normalized,
        "key":         cache_key(normalized),
    })


print("=" * 90)
print("SEQUENTIAL CACHE SIMULATION WITH FIX 1, 2, 3 (threshold = 85)")
print("=" * 90)

cache = {}  # normalized -> {idx, geocode_str, lookup_str}
results = []

for e in entries:
    idx         = e["idx"]
    norm        = e["normalized"]
    geocode_str = e["geocode_str"]
    lookup_str  = e["lookup_str"]

    # 1. Exact lookup
    if norm in cache:
        hit_idx = cache[norm]["idx"]
        results.append((idx, "EXACT HIT", hit_idx, 100.0, geocode_str, norm))
        continue

    # 2. Fuzzy lookup
    if cache:
        candidates = {k: k for k in cache}
        match = process.extractOne(
            norm,
            candidates,
            scorer=fuzz.token_sort_ratio,
            processor=utils.default_process,
            score_cutoff=THRESHOLD,
        )
        if match:
            matched_norm, score, _ = match
            hit_idx = cache[matched_norm]["idx"]
            results.append((idx, "FUZZY HIT", hit_idx, score, geocode_str, norm))
            continue

    # 3. Miss -> cache it
    cache[norm] = {"idx": idx, "geocode_str": geocode_str, "lookup_str": lookup_str}
    results.append((idx, "CACHE MISS", None, None, geocode_str, norm))


print(f"\n{'#':>3}  {'STATUS':<12}  {'MATCHES #':<10}  {'SCORE':>6}  LOOKUP STRING (ADDRESS ONLY)")
print("-" * 90)
for idx, status, hit_idx, score, geocode_str, norm in results:
    score_str  = f"{score:>5.1f}" if score is not None else "     -"
    hit_str    = f"#{hit_idx:<8}" if hit_idx else "          "
    flag = "NEW " if status == "CACHE MISS" else "    "
    print(f"{idx:>3}  {flag}{status:<12}  {hit_str}  {score_str}  {norm[:60]}")

misses = [r for r in results if r[1] == "CACHE MISS"]
hits   = [r for r in results if r[1] != "CACHE MISS"]

print("\n" + "=" * 90)
print(f"  Total addresses  : {len(results)}")
print(f"  Cache MISSes     : {len(misses)}  (API calls made)")
print(f"  Cache HITs       : {len(hits)}")
exact_hits = sum(1 for _, s, *_ in results if s == "EXACT HIT")
fuzzy_hits = sum(1 for _, s, *_ in results if s == "FUZZY HIT")
print(f"    Exact hits     : {exact_hits}")
print(f"    Fuzzy hits     : {fuzzy_hits}")
print(f"  API calls saved  : {len(hits)}")
print("=" * 90)

groups = {}
head_map = {}

for idx, status, hit_idx, score, geocode_str, norm in results:
    if status == "CACHE MISS":
        groups[idx] = [idx]
        head_map[idx] = idx
    else:
        head = head_map.get(hit_idx, hit_idx)
        groups.setdefault(head, [head])
        if idx not in groups[head]:
            groups[head].append(idx)
        head_map[idx] = head

print("\nGROUPS (addresses sharing the same cache entry):")
print("-" * 90)
for head_idx, members in groups.items():
    if len(members) == 1:
        continue
    member_info = {r[0]: r[4] for r in results}
    print(f"\n  Group (head=#{head_idx}):")
    for m in members:
        marker = " [FIRST/CACHED]" if m == head_idx else ""
        print(f"    #{m:>2}{marker}  {member_info[m][:75]}")
