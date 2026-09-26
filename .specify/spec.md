# Feature Specification: Address Resolution Waterfall & Post-Delivery Audit Loop

**Status:** Draft — for review
**Repo:** Route-Optimization (FastAPI backend on Cloud Run; BigQuery + Firestore; OR-Tools TSP)
**Scope:** Two functional areas — (A) Address-to-Waypoint Resolution (upstream, save-consignments path) and (B) Post-Delivery Closed-Loop Audit (downstream, sorting/delivery-completion path).
**Out of scope:** "Anchor & Hitchhiker" high-priority routing — see §6 Future Backlog.

Every requirement below carries an explicit **Blast Radius** — the files/collections/tables it touches, and, just as important, what it does *not* touch. This is documentation for implementers and reviewers, not a request for sign-off on the boundary itself.

## 0. Assumptions made explicit (read before implementing)

The originating problem statement referenced three things that do not exist in this repository as named. Rather than block drafting on them, this spec makes the following calls and flags them for review at `/speckit.clarify` or in PR review:

1. **`drs_address_memo`** does not exist today. The only related module is `drs_starting_point` (`app/db/firestore.py`), which stores one hub/depot point per DRS — not a per-address immutable pin store. This spec treats the "master waypoint" store as **net-new** (§2), not an extension of `drs_starting_point`.
2. **Commit `af5c6e7`**, cited as the regression this spec guards against, does not exist in this repo's git history (24 commits total, none matching). The *invariant* it was meant to protect ("a confirmed/verified pin must never be silently overwritten") is kept and enforced in §2.3; the specific commit citation is dropped as unverifiable.
3. **The post-delivery audit loop (§5)** assumes a driver-facing mobile client that does not exist in this repo (this service is backend-only). This spec defines the **backend ingestion contract** (endpoint + schema + processing rules) that such a client would call; building the mobile client itself is out of scope for this repo.

## 1. Functional Area A — Address-to-Waypoint Resolution (Upstream)

Applies to the `save-consignments` path (`app/services/save.py`, `app/services/address.py`, `app/services/geocoding.py`, `app/db/geocache.py`).

### 1.1 Vector search evaluation — `text-embedding-005` in BigQuery (BQML)

**Requirement:**
- Evaluate `ML.GENERATE_EMBEDDING` (model `text-embedding-005`, `task_type = 'SEMANTIC_SIMILARITY'`) via a BQML remote model, with candidate matching via `VECTOR_SEARCH` / `ML.DISTANCE` (cosine).
- **Hard constraint:** vector search MUST NOT run unconstrained across the whole address space. It is a *secondary fuzzy matcher*, scoped to candidates already isolated by postal code or Geohash-6 partition. It runs strictly **after** exact/fuzzy token matching in the master-waypoint tier (§2) and `geocache.py`'s existing exact/fuzzy lookup have both missed — never before, and never as the sole signal.
- Rationale for the constraint: pure semantic embeddings encode linguistic similarity, not physical proximity — e.g. "Flat 201, Sobha Quartz, Bellandur" and "Flat 201, Green Glen Residency, Bellandur" embed nearly identically despite being hundreds of metres apart, while "Sector 1" vs "Sector 2" can be separated by a physical corridor despite high textual similarity. Geohash/pincode pre-partitioning is the guard against this.
- This is an **evaluation**, not a committed production path: deliverable is a BQML pipeline + offline precision/recall report against a labeled sample of ambiguous addresses, gating a later decision to wire it into the live save-consignments flow.

**Blast radius:**
- New: a BigQuery dataset/table for embeddings (e.g. `address_embeddings`), a BQML remote model resource, a Vertex AI connection (external GCP resource, provisioning is an infra prerequisite, not app code).
- New: an offline/batch evaluation script (outside the request-serving path), e.g. `scripts/evaluate_embedding_match.py` or similar — not part of `app/`.
- Unchanged in this phase: `app/services/geocoding.py`, `app/services/address.py`, `app/db/geocache.py`, `app/services/save.py` request path. No production code calls `ML.GENERATE_EMBEDDING` until the evaluation gates a follow-up spec/PR.
- Cost note: BQML remote model calls are billed per invocation — this is why the requirement mandates geohash/pincode pre-scoping rather than corpus-wide search.

### 1.2 Master waypoint tier ($0 cost target)

**Requirement:**
- Introduce a matching tier that intercepts high-density, recurring, unambiguous destinations (large apartment complexes, SEZs, tech parks — e.g. "Sobha Dream Acres", "Prestige Tech Park", "Manyata") via **exact alias matching against a local cache**, before any external geocoding API call (Google Places) and before the BQML vector-search evaluation path (§1.1).
- This tier sits in the existing resolution order as the **first** check, ahead of `geocache.py`'s exact/fuzzy lookup: master-waypoint alias hit → `geocache.py` exact/fuzzy → (optionally, once evaluated) vector search within partition → external geocoding API (§1.3).
- Target cost for a master-waypoint hit is $0 — no external API call, no BQML invocation.

**Blast radius:**
- New: `app/db/master_waypoint.py` (mirroring `geocache.py`'s structure — normalize → alias-key lookup → return verified lat/lon) and a new Firestore collection, e.g. `master_waypoints`, keyed by normalized building/complex alias.
- Changed: `app/services/address.py` or `app/services/save.py` — insert the master-waypoint lookup as the first step in the resolution waterfall, ahead of the existing `geocache.get_cached_geocode_with_outcome` call.
- Unchanged: `app/db/geocache.py` internals, `app/services/geocoding.py`, `app/services/tsp.py` — this tier only short-circuits *before* they run; it does not modify their logic.
- Seeding: initial population of `master_waypoints` (e.g. from historically verified high-frequency addresses) is a data/ops task, not covered by this spec's code changes.

### 1.3 Immutability of confirmed manual pins

**Requirement:**
- Entries in the master-waypoint store (§1.2) that are marked confirmed/verified (whether seeded manually or promoted via §5.2's 3-confirmation rule) are **immutable anchors**: no code path in the address-resolution waterfall or in route optimization (`app/services/tsp.py`, `app/services/sorting.py`) may overwrite or re-geocode them.
- This must be enforced at the write layer (e.g. a guard in `master_waypoint.py`'s save function that refuses to overwrite a `verified: True` record except via the explicit promotion/correction path in §5.2), not just by convention.

**Blast radius:**
- Changed: the save/update function in the new `master_waypoint.py` module only (§1.2). No change to `app/services/tsp.py` or `app/services/sorting.py` — they only ever *read* resolved coordinates, they don't write them, so the immutability guard lives entirely in the write path, not the consumers.

### 1.4 External geocoding ambiguity handling

Applies when an address misses both the master-waypoint tier (§1.2) and `geocache.py`, and falls through to the external Google Places API call in `app/services/geocoding.py`, bounded by `country:IN`, target PIN code, and depot bounds.

> Note on current implementation: `app/services/geocoding.py` calls the **Google Places API v1 text-search** endpoint (`places.googleapis.com/v1/places:searchText`), not the classic Geocoding API (`GEOCODE_URL` is defined in `app/config.py` but unused today). The three scenarios below are specified against whichever endpoint is actually wired in; if a switch to the classic Geocoding API is intended, that is a separate decision this spec does not make.

**1.4.1 Scenario 1 — Blank / `ZERO_RESULTS`**
- Cause: over-specified customer descriptions (e.g. "Flat 402, 3rd Floor, Wing B, Opp Water Tank, Behind SLV").
- Action: progressive degradation — strip micro-tokens (flat, room, floor, wing, "opp", "behind") and re-query using `[Building/Society Name] + [Sub-locality] + [Pincode]`. If still blank: set `geocode_status = 'NEEDS_HILT'`, `geocode_error = 'ZERO_RESULTS'`.
- **Blast radius:** new token-stripping helper in `app/services/address.py` (extends existing address-cleaning logic); new fields `geocode_status`, `geocode_error` added to the consignment record schema — touches `app/schemas.py`, the BigQuery `consignments_routing` MERGE in `app/db/bigquery.py`, and the Firestore consignment doc shape in `app/db/firestore.py`. `app/services/tsp.py` unchanged directly, but §1.4.3 below governs how `sorting.py` must treat these flagged records.

**1.4.2 Scenario 2 — Multiple candidates, out of bounds**
- Cause: common street names (e.g. "1st Main Road") resolving across multiple distant sectors.
- Action: spatial boundary clamping — discard any returned candidate whose coordinates fall outside the operational delivery polygon or target PIN code before candidate selection.
- **Blast radius:** new bounds-check function in `app/services/geocoding.py` (or a new `app/services/geo_bounds.py`), applied to the raw API response before the existing best-candidate selection logic. No schema change if a valid in-bounds candidate remains after clamping (normal flow continues).

**1.4.3 Scenario 3 — Persistent multi-match, inside bounds (anti-guessing quarantine)**
- Cause: ambiguous phases or adjacent sub-divisions (e.g. "Phase 1" vs "Phase 2") that survive bounds-clamping with more than one plausible in-bounds candidate.
- Action: do not guess. Extract all candidates `(lat, lon, formatted_address, place_id)`, set `geocode_status = 'NEEDS_HILT'`, `geohash_group_id = 'UNASSIGNED'`.
- **Critical downstream requirement:** `app/services/sorting.py` (which calls `app/services/tsp.py`) MUST filter out any record with `geohash_group_id = 'UNASSIGNED'` before building the TSP input set. `tsp.py` itself is not modified — it should never receive an unresolved/ambiguous coordinate in the first place; the filter belongs in `sorting.py`'s existing active/pending split logic.
- **Blast radius:** changed — `app/services/geocoding.py` (candidate extraction + status setting), `app/schemas.py` (candidate list field), BigQuery/Firestore schema (`geocode_status`, `geohash_group_id = 'UNASSIGNED'`, candidate array), and `app/services/sorting.py` (exclusion filter ahead of the `solve_route_order` call). `app/services/tsp.py` itself: **unchanged** — this requirement protects it by construction (bad input never reaches it), not by adding defensive code inside it.
- Dispatcher review path (how a `NEEDS_HILT` record gets resolved back to a real geohash group) is referenced but its UI/workflow is out of scope for this spec; only the data contract (`geocode_status`, `geohash_group_id`) is specified here.

## 2. Functional Area B — Post-Delivery Closed-Loop Audit (Downstream)

### 2.1 Audit trigger (Δ > 500m)

**Requirement:**
- On delivery completion, compute the Haversine distance between the planned waypoint coordinates (as resolved by §1) and the driver's actual completion GPS coordinates.
- If Δ > 500m, create an audit record in BigQuery scoped to `(consignmentId, drsNo)`.

**Blast radius:**
- New: a BigQuery table, e.g. `delivery_audit`, and a write path for it — either a new function in `app/db/bigquery.py` or a new module `app/db/audit.py`.
- Reused: `haversine_distance` already exists in `app/services/tsp.py` (line 17) — this requirement reuses that function rather than reimplementing distance math; if reuse would create an awkward dependency (tsp.py is otherwise route-solving, not audit), an equivalent shared helper may be extracted to a common module instead. Either way, no *behavioral* change to `tsp.py`'s existing TSP-solving code.
- New: the trigger point itself needs a call site — this repo currently has no delivery-completion endpoint (no mobile client posts completion events here today; see §0.3). This spec defines the check's logic; wiring it to a real completion event requires the new endpoint in §2.2.

### 2.2 Driver micro-survey ingestion

**Requirement:** capture an operational reason code from the mobile client alongside (or instead of) the raw completion event, with the following handling:

| Code | Meaning | Action |
|---|---|---|
| `A` | At Door / Gate — waypoint was inaccurate | Route to review queue; **auto-update** master waypoint coordinates after **3 distinct confirmations** of the same corrected location |
| `B` | Customer met driver elsewhere (ad-hoc handover) | Retain existing waypoint; record as a tribal-knowledge note (no coordinate change) |
| `C` | Marked delivered later / app lag | Retain waypoint; flag driver telemetry metrics for sync-lag analysis (no coordinate change) |
| `D` | Map pin completely wrong | Quarantine the waypoint immediately for dispatcher review (do not wait for 3 confirmations) |
| *(omitted)* | App didn't send feedback | Record `NOT_PROVIDED`; **must not block** consignment completion |

**Blast radius:**
- New: a request schema (`app/schemas.py`) for the micro-survey payload, and a new endpoint (e.g. `POST /consignments/{id}/delivery-feedback` under a new or existing router) — this is the backend contract a mobile client would call; the mobile client itself is out of scope (§0.3).
- New: the "3 distinct confirmations → auto-update" logic lives in `app/db/master_waypoint.py` (§1.2/§1.3) as the **only** sanctioned path that may overwrite a verified pin — this is the explicit exception referenced in §1.3's immutability guard, not a bypass of it.
- New: `D` (quarantine) writes to the same "needs dispatcher review" status used by §1.4.3 (`geocode_status = 'NEEDS_HILT'`), reusing that status vocabulary rather than inventing a parallel one.
- Unchanged: `app/services/tsp.py`, `app/services/sorting.py` — this endpoint is post-delivery; it does not sit in the route-solving request path. A quarantined pin only affects *future* resolutions of that address, handled by §1's waterfall reading `geocode_status` on the next save-consignments run.

## 3. Cross-cutting data contract additions

Consolidated list of new fields this spec introduces (for schema-review convenience — each is also called out in its owning requirement above):

- `geocode_status` (nullable enum: e.g. `RESOLVED`, `NEEDS_HILT`) — §1.4.1, §1.4.3
- `geocode_error` (nullable string, e.g. `ZERO_RESULTS`) — §1.4.1
- `geohash_group_id` may take the sentinel value `'UNASSIGNED'` in addition to its existing `<drsNo>_<geohash>` form — §1.4.3
- `master_waypoints` collection (Firestore): alias, lat/lon, `verified` bool, `confirmation_count` — §1.2, §1.3, §2.2
- `delivery_audit` table (BigQuery): `consignmentId`, `drsNo`, planned coords, actual coords, delta metres, reason code, timestamp — §2.1, §2.2

## 4. Explicitly not changed by this spec

To bound the blast radius of the overall effort:
- `app/db/geocache.py` — reused as-is (its exact/fuzzy address matching is unaffected; it simply becomes one tier in a longer waterfall).
- `app/services/tsp.py` — reused as-is. No new logic is added inside it; both §1.4.3 and §2.1 are careful to keep bad/ambiguous/audit data out of its input rather than adding conditionals inside the solver.
- `app/db/firestore.py`'s `drs_starting_point` handling — untouched; it remains the per-DRS hub point lookup it is today, separate from the new per-address `master_waypoints` collection.
- No mobile client code (doesn't exist in this repo; §2.2 defines the contract only).

## 5. Future Backlog / Next Phase (out of scope here)

- **Anchor & Hitchhiker** high-priority routing strategy — not yet implemented, not specified here. Any future spec for it should account for its interaction with the master-waypoint tier (§1.2) and the `NEEDS_HILT`/`UNASSIGNED` quarantine states (§1.4.3), since both affect which stops are eligible for route optimization.

## 6. Open items for `/speckit.clarify` or PR review

1. Confirm the `master_waypoints` Firestore collection name and whether it should instead live in BigQuery (this spec assumes Firestore, matching `geocache.py`'s pattern, for low-latency exact lookup in the request-serving path).
2. Confirm whether §1.1's BQML evaluation is time-boxed / has an owner and a decision deadline, since it is explicitly not wired into production in this spec.
3. Confirm the dispatcher-review workflow/UI for `NEEDS_HILT` records — referenced but not specified here (data contract only).
4. Confirm §2.2's new endpoint's auth/identity model for the mobile client (out of scope for backend-only repo, but the contract should note what it expects).
