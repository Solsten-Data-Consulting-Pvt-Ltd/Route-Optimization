# Route-Optimization — Handover

As of **5 Oct 2026**. Written for whoever picks this repo up next. It covers what the backend does, what was built in the address-resolution workstream, what is merged, what isn't, and what still needs doing. For the full reference, see `README.md`. For the design intent behind §5.x, see `.specify/spec.md` on `claude/cloud-browser-task-pgrfzz`.

---

## 1. What this service does

A FastAPI service on Cloud Run. It turns scanned consignments into **pins** (lat/lon) and turns each driver's DRS into an **ordered route**. Pushing to `dev` deploys to dev, and pushing to `main` deploys to prod (`.github/workflows/deploy-*.yml`).

| Method | Path | What it does |
|---|---|---|
| `POST` | `/geocode/preview` | Resolves a pin for an address at scan time and shows it to the executive. Writes nothing to the routing stores |
| `POST` | `/save-consignments` | Geocodes consignments (or takes the executive's confirmed pin) and saves them to BigQuery + Firestore `consignments_routing` |
| `POST` | `/run-sorting` | Orders a DRS's stops (geohash clusters + OR-Tools TSP). Delivered stops keep their slot |
| `GET` | `/health`, `/docs`, `/redoc` | Liveness, OpenAPI |

> `README.md`'s endpoint table doesn't list `/geocode/preview` yet; it's described further down the README.

### How a pin is found: the lookup order (`app/services/save.py::_geocode`)

| # | Tier | Where | Notes |
|---|---|---|---|
| 0 | Executive-confirmed pin | `confirmedLocations` in the save request | Accepted/corrected on the scan screen. No lookup, but reconciled with the DRS memo |
| 1 | DRS memo, **corrected** entries | Firestore `drs_address_memo` | A correction made in this DRS today beats everything |
| 2 | Verified geocode cache | Firestore `geocode_cache` (`verified: true` only) | Exact, then fuzzy `token_sort_ratio ≥ 85`. Entries are verified by executives at end of DRS |
| 3 | DRS memo, any entry | Firestore `drs_address_memo` | Same receiver/place earlier in **this** DRS (phone, door numbers, text containment/fuzzy) |
| 4 | Google Places text search | `app/services/geocoding.py` | Result saved to `geocode_cache` as `verified: false` and to the memo |

Embeddings (§5.5) are **not** in this order. They exist only as an offline evaluation (section 4.4).

### Where data lives

| Store | R/W | What |
|---|---|---|
| Firestore `consignments` | read | Source documents: `receiver.address` (OCR-formatted), `receiver.fullAddress` (raw label), `receiver.phone`, `receiver.addressComponent` |
| Firestore `geocode_cache` | read + write | Address → pin. Served only when `verified: true`. Carries `drs_memo_group(s)` so end-of-day verification covers every spelling of a place |
| Firestore `drs_address_memo` | read + write | One doc per DRS with every pin resolved in it. **Needs a TTL policy on `expires_at`** (2 days) |
| Firestore `featureFlags` | read | 3PL's flag collection; `geocodeResolutionV2`. Cached 45 s; missing/error = off |
| Firestore `consignments_routing` | write | Mirror of the BigQuery row for the apps, plus `geocode_candidates` for doubtful pins (Firestore only) |
| Firestore `drs_cache_metrics` | write | Per-DRS cache stats per save (exact/fuzzy/DRS-memo hits, misses, API calls) |
| Firestore `drs_starting_point` | read | Hub/depot per DRS |
| BigQuery `Hermes_Exports.consignments_routing` | read + write | System of record: pins, geocode fields, `geocode_resolution`, `geocode_source`, clusters, sequence |
| BigQuery `consignments_structured` | read | Delivery status, so sorting keeps delivered stops in place |

---

## 2. What was built (address-resolution workstream)

### 2.1 DRS address memo: merged (`b717c4d`, then many fixes on `dev`)

**Problem.** Executives verify `geocode_cache` only at end of DRS, so parcel #3 to the same receiver as parcel #1 missed the cache and called Places again. That cost money and sometimes gave a different pin.

**What it does.** Every pin resolved in a DRS is remembered (`app/db/drs_memo.py`). Later consignments in the same DRS reuse it when `app/services/address_match.py::drs_match` says they're the same place:

1. Different pincode → never the same place.
2. Same `receiver.phone` → same place, unless door numbers conflict. Since `6ef025c` the text must also be at least 60 % similar.
3. A road-level address (no door number, under 3 distinctive words, e.g. "Kodathi Village Main Road …") → never matched on text alone.
4. Otherwise door numbers must agree, and the text must be equal, one containing the other (≥ 90 %), or fuzzy ≥ 90.

New spellings that reuse a memo pin get their own unverified `geocode_cache` doc tagged `drs_memo_group`. `geocache.verify_cache_group()` verifies a whole group at end of DRS, so every spelling is served from the next day.

**Later fixes on `dev`, by others:**

- `7c781f0`: unit suffixes ("17A" vs "17").
- `4d6d14f`: a door-number subset with the same phone counts as the same place.
- `916ce54`, `dfe5d0f`: group bookkeeping and chunked verification.
- `b76e4f8`, `921ae3e`: company-name and OCR locality spellings.
- `8d6eae1`: spaces around "/" in door numbers.
- `04a2a24`, `6ef025c`: building-level matching using `receiver.address`; a shared phone also needs similar text.
- `d563b78`: memo hits counted in `hitRate`.
- `330b3b0`: a second human correction wins a priority tie.

### 2.2 Address resolution: merged to `main` and `dev` (`ec0c982`, `7b648de`)

Every geocode result now carries three fields. The codes are in `app/services/geocode_codes.py`, and 3PL reads these exact names:

- `geocode_status`: `success` · `needs_review` · `failed`
- `geocode_resolution`: `KNOWN_GOOD` · `CONFIDENT` · `ZERO_RESULTS` · `MISSING_ADDRESS` · `SERVICE_ERROR` · `MULTI_CANDIDATE` · `PINCODE_MISMATCH`
- `geocode_source`: `confirmed` · `preview` · `admin` · `memo_corrected` · `memo` · `cache_exact` · `cache_fuzzy` · `places` · `places_retry`

Behind **`featureFlags/geocodeResolutionV2`** (default off):

- Places is asked for up to 5 candidates. Candidates in the address's pincode are preferred, and in-pincode candidates more than 300 m apart give `MULTI_CANDIDATE`.
- One retry with a shortened address (`address.build_retry_address`) runs before `ZERO_RESULTS`.
- `MULTI_CANDIDATE` and `PINCODE_MISMATCH` rows become `needs_review`. They are **still routed**.
- `geocode_error` uses one standard message per resolution.
- An accepted preview pin carries its `resolution`, `source`, `pincode` and (`7b648de`) `candidates` into the save. The candidates are written to Firestore `consignments_routing.geocode_candidates` for admin review.

**With the flag off**, pin choice, status, error text, exception flag and pincode are exactly as before. Only `geocode_resolution` and `geocode_source` are filled in, so the distribution can be measured first.

### 2.3 Neighbouring pincodes: **not merged** (`5af8b8b` on `feature/address-resolution-pincodes`)

**Problem.** Customers write the pincode next door. Shahi Exports in Ambalipura is 560103 but is often labelled 560102. With the flag on, the classifier preferred *any* place in 560102 over Google's real match, and the verified address went back to review on every cache hit.

**What it adds:**

- **`app/data/pincode_neighbours.json` + `app/services/pincode_neighbours.py`:** reviewed, symmetric neighbour pairs, seeded with 560102 ↔ 560103.
- **`classify_places`:** a candidate in a neighbouring pincode counts as in-area. If Google's best match is outside the written pincode and the best in-pincode candidate is over 1 km from it (`TOP_CANDIDATE_CONFLICT_M`), the result is `MULTI_CANDIDATE`, keeping Google's pick when it is in a neighbour.
- **Cache and memo hits:** a neighbouring pincode, or a pincode a person already accepted for that place (`geocode_cache.accepted_pincodes`, written by `geocache.accept_pincode()` when an executive keeps a mismatched pin), is not `PINCODE_MISMATCH`.
- **DRS memo:** a match across neighbouring pincodes needs the same receiver phone.

19 tests are in `tests/test_pincode_neighbours.py`.

**Merge status:** it conflicts with `dev` in one file, `app/services/address_match.py`. `dev` has since rewritten `drs_match` (the subset rule and the phone + text rule), so the neighbour-plus-phone rule has to be re-applied by hand inside the new `drs_match`.

### 2.4 §5.5 BQML embeddings evaluation: **not merged** (`505bd3f`, `0e42357` on `feature/bqml-embeddings-eval`)

**What it is.** An **offline** experiment, never part of the deployed app. The Dockerfile copies only `app/`. It answers one question: when master-waypoint and `geocode_cache` both miss, can a text-embedding match inside the address's pincode find the same verified place without wrong-building matches?

**Pipeline** (`offline_eval/bqml_embeddings/`, runbook in its `README.md`):

| Step | Does |
|---|---|
| `setup` | Creates dataset `route_opt_eval` + BQML remote model over Vertex `text-embedding-005` |
| `corpus` | Collects addresses whose true pin is known: `delivery_audit` (§5.4) when it exists, otherwise executive-corrected pins (`locationOverridden`) |
| `neighbours` | Learns neighbouring-pincode pairs from data + the reviewed seed file |
| `prior` | Marks which addresses master-waypoint or `geocode_cache` would already have answered |
| `embed` | `ML.GENERATE_EMBEDDING` (`SEMANTIC_SIMILARITY`), incremental |
| `evaluate` | Replays the last 30 days against addresses known before each scan, **only inside the pincode / pincode+neighbours / geohash-6 partition**; precision/recall per cut-off |
| `report` | Markdown + CSV + every wrong-building match, plus a recommended cut-off (≥ 99.5 % precision) or "do not wire in" |

**Guarantees, enforced by `tests/test_bqml_embeddings_eval.py`:**

- no unscoped `ML.DISTANCE` / `VECTOR_SEARCH`;
- no peeking at later data;
- nothing in `app/` references it;
- it refuses to run unless its flag is on (`--dry-run` only prints SQL).

The code creates all its own BigQuery objects. By hand you only create the Vertex AI connection (+ `roles/aiplatform.user`) and turn the flag on.

**Status:** never run against BigQuery. The SQL is only syntax-checked.

**Merge status:** it can't be merged into `dev` as is. It was built on `claude/cloud-browser-task-pgrfzz`, the spec branch, whose `app/` is older than `dev`:

- the spec branch's `app/db/feature_flags.py` reads Firestore `app_config`, while `dev`'s reads `featureFlags`;
- the runner imports `app/db/master_waypoint.py`, which exists only on the spec branch.

To land it, move only its two commits onto `dev` and adapt:

- point the flag check at `dev`'s `featureFlags`;
- make the master-waypoint part of the prior-tier check optional;
- drop the spec-branch `app/` differences.

---

## 3. Branches

| Branch | State | Contains |
|---|---|---|
| `main` / `dev` | Deployed (prod / dev) | Memo (2.1) + address resolution (2.2). `dev` has the extra memo fixes |
| `feature/address-resolution` | Pushed, merged | 2.2 (`ec0c982`) |
| `feature/address-resolution-pincodes` | Local only | 2.2 + neighbouring pincodes (`5af8b8b`). Needs rebasing onto `dev` (one conflict) |
| `feature/bqml-embeddings-eval` | Pushed, not merged | §5.5 evaluation on top of the spec branch. Needs porting to `dev` |
| `claude/cloud-browser-task-pgrfzz` | Pushed | `.specify/spec.md` (single source of truth for §5.x), master-waypoint tier (§5.3), `app_config` flags (§5.6), on an older `app/` |

---

## 4. One-time ops checklist

| Item | Where | Status |
|---|---|---|
| `ALTER TABLE … ADD COLUMN geocode_resolution STRING, geocode_source STRING` | BigQuery `consignments_routing`, dev **and** prod | **Confirm.** `ec0c982` is on `main` and `dev`, both auto-deploy, and the MERGE writes these columns. If they're missing, every save fails |
| TTL policy on `drs_address_memo.expires_at` | Firestore | Confirm. Without it, old memo docs are never cleaned up |
| `featureFlags/geocodeResolutionV2` | Firestore | Off by default. Measure `geocode_resolution` for 2–3 days, deploy the 3PL side, then turn on in dev → prod |
| Vertex AI connection `vertex_embeddings` + `roles/aiplatform.user` | GCP, same region as `Hermes_Exports` | Only needed to run §5.5 |
| Flag for §5.5 | Firestore | `app_config/bqml_embeddings_eval` on the spec branch; `featureFlags` once ported to `dev` |

---

## 5. How to test

```bash
python -m venv .venv && source .venv/Scripts/activate      # Git Bash on Windows
pip install -r requirements.txt
python -m unittest discover -s tests                        # no GCP access needed
```

- Tests use in-memory fakes and touch no Google services. `tests/__init__.py` makes every feature flag read as off.
- **§5.5 evaluation (on its branch):** `python -m offline_eval.bqml_embeddings.run_eval --location <region> --dry-run` prints the SQL. A real run on dev goes step by step, as in `offline_eval/bqml_embeddings/README.md`.

---

## 6. Open items / next steps

1. **Merge neighbouring pincodes** (2.3): rebase `feature/address-resolution-pincodes` onto `dev` and re-apply the neighbour-plus-phone rule in the new `drs_match`.
2. **Port the §5.5 evaluation onto `dev`** (2.4), then do a first run on dev.
3. **Delivery GPS as labels.** Drivers don't record reason codes yet. Delivery GPS (delivered near the pin, or repeat deliveries agreeing) would give many more right answers. This needs the exact field path for the delivery location in Firestore `consignments`.
4. **End-of-day verified `geocode_cache` as labels** for the evaluation. Today they only mark what the cache would already answer.
5. **§5.4 EOD feedback endpoint** (`POST /eod-delivery-feedback` → BigQuery `delivery_audit`), plus the mobile-app change to send reason codes A–D. The evaluation already expects the column names listed in the spec and its README.
6. **Automation:** after a successful manual §5.5 run, a weekly Cloud Run Job + Cloud Scheduler run (still flag-gated). Pincode-pair learning could be a BigQuery scheduled query.
7. **README:** add `/geocode/preview` to the endpoint table.

---

## 7. Glossary

| Term | Meaning |
|---|---|
| DRS | Driver Route Sheet: one driver's consignments for a day (`drsNo` / `drsId`) |
| EOD | End of day: executives verify pins; drivers' debrief (>500 m question) in the mobile app |
| DRS memo | Per-DRS memory of resolved pins, so same-place parcels reuse one pin |
| Verified cache | `geocode_cache` entries with `verified: true`, the only ones ever served |
| Master waypoint | Curated, verified anchor address (§5.3, spec branch), checked before the cache |
| `needs_review` | Pin saved and routed, but flagged for admin (multi-candidate or pincode mismatch) |
| Neighbouring pincode | A pincode sharing a border with the written one (e.g. 560102 ↔ 560103) |
| Embedding | 768 numbers describing an address's meaning; similar addresses get close vectors |
