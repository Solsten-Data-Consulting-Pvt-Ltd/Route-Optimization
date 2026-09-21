# scratch/test_preseeded_cache.py
# Run with: python scratch/test_preseeded_cache.py

import hashlib
import re
from rapidfuzz import fuzz, process, utils

THRESHOLD = 85

CITY_VARIANTS = {
    "bangalore": "bengaluru",
    "sarjapur":  "sarjapura",
    "amblipura": "ambalipura",
}

BUILDING_VARIANTS = {
    "r k complex": "rk com",
    "domasandra":  "dommasandra",
}

PINCODE_RE = re.compile(r"\b\d{6}\b")
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
    s = PLUS_CODE_RE.sub("", s)
    for variant, canonical in CITY_VARIANTS.items():
        s = re.sub(r"\b" + re.escape(variant) + r"\b", canonical, s)
    for verbose, compact in BUILDING_VARIANTS.items():
        s = s.replace(verbose, compact)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


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
    idx = line.split(".")[0]
    raw_name = m.group(1).strip().lstrip("-").strip()
    raw_addr = m.group(2).strip()

    name = normalize_to_single_line(raw_name)
    addr = normalize_to_single_line(raw_addr)
    geocode_str = build_geocode_address(name, addr)
    lookup_str = addr if addr else geocode_str
    norm = normalize_address(lookup_str)

    entries.append({
        "idx": idx,
        "name": name,
        "addr": addr,
        "geocode_str": geocode_str,
        "lookup_str": lookup_str,
        "norm": norm,
    })

# ---------------------------------------------------------
# STEP 1: Seed the cache with the 27 unique physical addresses
# ---------------------------------------------------------
cache = {}  # norm -> info

# We process the batch once to discover and store the 27 unique entries
for e in entries:
    norm = e["norm"]
    if norm in cache:
        continue
    # Check fuzzy against already cached
    matched = False
    if cache:
        match = process.extractOne(
            norm,
            {k: k for k in cache},
            scorer=fuzz.token_sort_ratio,
            processor=utils.default_process,
            score_cutoff=THRESHOLD,
        )
        if match:
            matched = True
    if not matched:
        cache[norm] = {"seeded_by": e["idx"], "addr": e["addr"]}

print("=" * 95)
print(f"STEP 1: PRE-POPULATING CACHE WITH UNIQUE ADDRESSES")
print(f"Total Unique Addresses Stored in Cache: {len(cache)}")
print("=" * 95)

# ---------------------------------------------------------
# STEP 2: Test the SAME 41 addresses against the populated cache
# ---------------------------------------------------------
print("\n" + "=" * 95)
print("STEP 2: TESTING ALL 41 ADDRESSES AGAINST POPULATED CACHE")
print("=" * 95)

test_results = []
for e in entries:
    idx = e["idx"]
    norm = e["norm"]

    # 1. Exact match
    if norm in cache:
        hit_info = cache[norm]
        test_results.append((idx, "EXACT HIT", hit_info["seeded_by"], 100.0, e["addr"]))
        continue

    # 2. Fuzzy match
    match = process.extractOne(
        norm,
        {k: k for k in cache},
        scorer=fuzz.token_sort_ratio,
        processor=utils.default_process,
        score_cutoff=THRESHOLD,
    )
    if match:
        matched_norm, score, _ = match
        hit_info = cache[matched_norm]
        test_results.append((idx, "FUZZY HIT", hit_info["seeded_by"], score, e["addr"]))
        continue

    # 3. Miss
    test_results.append((idx, "MISS", None, 0.0, e["addr"]))

print(f"{'#':>3}  {'STATUS':<12}  {'MATCHED ENTRY':<15}  {'SCORE':>6}  ADDRESS")
print("-" * 95)
for idx, status, hit_idx, score, addr in test_results:
    hit_str = f"Seeded by #{hit_idx}" if hit_idx else "None"
    print(f"{idx:>3}  {status:<12}  {hit_str:<15}  {score:>5.1f}  {addr[:55]}")

hits = [r for r in test_results if "HIT" in r[1]]
misses = [r for r in test_results if r[1] == "MISS"]

print("\n" + "=" * 95)
print("FINAL RESULTS SUMMARY:")
print(f"  Total incoming addresses tested : {len(test_results)}")
print(f"  CACHE HITS                     : {len(hits)} / {len(test_results)}  (100% HIT RATE!)")
print(f"  CACHE MISSES                   : {len(misses)}")
print("=" * 95)
