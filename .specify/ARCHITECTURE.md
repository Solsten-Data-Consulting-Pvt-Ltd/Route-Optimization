# Route-Optimization — Current Architecture

**Scope of this document:** describes the codebase as it stands on branch `claude/cloud-browser-task-pgrfzz` (PR #3), including the tech debt inventory and the feature-flag/master-waypoint work added this session. Where `main` has diverged with additional, not-yet-merged work (`drs_memo.py`, `address_match.py`), it's called out explicitly rather than silently ignored or silently merged in — see §9.

---

## 1. One-paragraph summary

A single FastAPI service on Cloud Run, replacing two legacy Cloud Functions (`save_consignments`, `run_sorting`). It has two jobs: geocode delivery addresses and persist them with coordinates (Job 1), then solve a same-day route order for a driver's set of stops (Job 2). State lives in two places — Firestore for low-latency operational reads/caches, BigQuery as the system of record queried by MERGE/UPDATE SQL. No web UI; the "product" is the API plus what downstream systems build on it.

## 2. Runtime & deployment

| Aspect | Value |
|---|---|
| Language/framework | Python 3.11, FastAPI, `uvicorn` |
| Entry point | `app/main.py` → `uvicorn app.main:app` (`Procfile`, `Dockerfile`) |
| Hosting | Google Cloud Run, region `asia-south1`, two environments: `route-optimization-dev` and `route-optimization` (prod) |
| Deploy trigger | `.github/workflows/deploy-dev.yml` on push to `dev`; `deploy-prod.yml` on push to `main` |
| **Ingress auth** | Both deploys use `--no-allow-unauthenticated` — Cloud Run's IAM layer requires a valid Google-signed identity token to reach the service at all. This is infra-level auth, not app-level (see §7). |
| Secrets | `GOOGLE_MAPS_API_KEY` via `--set-secrets` (dev shown; prod likely equivalent) |
| Data project | `BQ_DATASET=Hermes_Exports`, `BQ_TABLE=consignments_routing`, `BQ_STRUCTURED_TABLE=consignments_structured`, same GCP project doubles as `FIRESTORE_PROJECT` |
| CI | `.github/workflows/ci.yml` — `python -m unittest discover -s tests -v` on every PR/push to `dev`/`main` |
| Dependencies | `fastapi`, `uvicorn[standard]`, `requests`, `pygeohash`, `google-cloud-firestore`, `google-cloud-bigquery`, `ortools`, `rapidfuzz==3.14.6` (see `requirements.txt`) |

## 3. Directory structure

```
app/
├── main.py              FastAPI app, CORS (wide open), /health, router mount
├── config.py             env-driven static config (project IDs, table names, API key,
│                          geohash lengths, cache TTLs) + this session's dynamic-flag TTL
├── schemas.py             Pydantic request models (thin — see §7 tech debt)
├── routers/
│   ├── __init__.py        api_router = save.router + sorting.router
│   ├── save.py             POST /save-consignments
│   └── sorting.py          POST /run-sorting
├── services/
│   ├── address.py           text normalization/cleaning, pincode extraction
│   ├── geocoding.py         Google Places API v1 text-search client
│   ├── save.py               Job 1 orchestration (the pipeline; see §5)
│   ├── sorting.py            Job 2 orchestration (the pipeline; see §6)
│   └── tsp.py                 haversine distance + OR-Tools open-path solver
└── db/
    ├── clients.py             lazy-singleton Firestore/BigQuery clients
    ├── bigquery.py              MERGE/UPDATE SQL against consignments_routing
    ├── firestore.py              consignments / consignments_routing / drs_starting_point
    ├── geocache.py                exact+fuzzy verified-address cache (existing, mature)
    ├── cache_metrics.py            per-DRS cache-hit analytics writer
    ├── feature_flags.py            NEW — dynamic flag store (§8)
    └── master_waypoint.py          NEW — master-waypoint tier (§8)
tests/                       unittest, no live GCP credentials needed (fakes throughout)
scratch/                     ad hoc manual test scripts, not part of CI
.specify/                    spec.md, HOW_TO.md, ARCHITECTURE.md (this file),
                              ARCHITECTURE_INVESTIGATION.md — see .specify/HOW_TO.md
```

Layering is strict and consistent: `routers/` (HTTP concerns, request validation, error shaping) → `services/` (business logic/orchestration) → `db/` (Firestore/BigQuery access, one module per collection/table family). Nothing in `db/` calls back up into `services/`, and routers never touch `db/` directly. This pattern held up cleanly when adding `feature_flags.py`/`master_waypoint.py` — both slot into `db/` and are consumed from `services/save.py` without bending the layering.

## 4. Data model

### Firestore (operational + cache)

| Collection | Written by | Read by | Purpose |
|---|---|---|---|
| `consignments` | (upstream, not this repo) | `app/db/firestore.py:get_consignments_by_id` | Source parcel records |
| `consignments_routing` | `firestore.upsert_consignments_routing` / `upsert_routing_from_sorting` | dispatcher tooling (external) | Mirrors the BigQuery row per consignment for low-latency reads |
| `drs_starting_point` | (ops, not this repo) | `firestore.get_drs_starting_point` | Per-DRS hub/depot coordinates |
| `geocode_cache` | `geocache.save_to_cache` | `geocache.get_cached_geocode_with_outcome` | Exact+fuzzy verified-address cache, keyed by a sha256 of the normalized address |
| `drs_cache_metrics` | `cache_metrics.write_drs_cache_metrics` | (analytics only) | One doc per save run, per DRS — hit-rate telemetry |
| `app_config` **(new)** | `feature_flags.seed_default_flags` / manual | `feature_flags.is_enabled/get_flag/get_value` | Dynamic feature flags (§8) |
| `master_waypoints` **(new)** | `master_waypoint.seed` / `record_confirmation` | `master_waypoint.lookup` | Curated high-confidence address anchors (§8) |

### BigQuery (system of record)

| Table | Role |
|---|---|
| `consignments_routing` | One row per consignment: address/geocode fields, `geocode_status`/`geocode_error`, `geohash_*` (locality/building/exact), `geohash_group_id`, `planned_*`/`actual_*` sequence fields, `is_active`. Written by `merge_routing_rows` (MERGE, from `save.py`) and `write_group_assignments` (UPDATE, from `sorting.py`). |
| `consignments_structured` | External export this repo only reads, joined against `consignments_routing` for delivery status (`fetch_active_rows_for_drs`). |

## 5. Job 1 — `save-consignments` pipeline

`POST /save-consignments` (`routers/save.py`) → `save_consignments_pipeline` (`services/save.py`):

1. Dedupe requested `consignmentIds`, fetch docs from Firestore (`get_consignments_by_id`).
2. Per consignment: build `geocode_address_str` (`build_geocode_address` — receiver name prefixed onto address unless already present).
3. **`_geocode(address, lookup_address)`** — the resolution waterfall, now three tiers deep:
   a. **NEW, flag-gated**: master-waypoint exact-alias lookup (`master_waypoint.lookup`) — $0 cost, only runs if `master_waypoint_v2` is enabled.
   b. `geocache` exact/fuzzy lookup (`get_cached_geocode_with_outcome`) — unchanged, existing.
   c. Google Places API v1 text search (`places_search_address`) — unchanged, existing; result saved back to `geocache` on success.
4. Build a BigQuery row (`build_row`) — geocode result + provenance fields + a geohash triplet (`pgh.encode`, 3 precisions).
5. Batch `merge_routing_rows` (BQ MERGE — update-if-matched, insert with `geohash_group_id='UNASSIGNED'` if not) then re-read and `upsert_consignments_routing` (Firestore mirror).
6. Per-DRS cache metrics (now including `masterWaypointHits`) written via `write_drs_cache_metrics` — failures here never fail the save (explicit try/except).

Does **not** touch `drs_starting_point` — that's read later, in Job 2, since it may not exist yet at save-time.

## 6. Job 2 — `run-sorting` pipeline

`POST /run-sorting` (`routers/sorting.py`) → `run_sorting_pipeline` (`services/sorting.py`):

1. Read the DRS hub (`get_drs_starting_point`), fetch all active rows for the DRS (`fetch_active_rows_for_drs`, joined to `consignments_structured` for delivery status).
2. Split delivered (`statusCode == "DE"`) vs. pending. Delivered rows keep their existing `actual_sequence_order` slot; only pending rows are re-optimized.
3. Filter pending rows to `usable_pending` — must have `latitude`/`longitude`/`geohash_exact_loc`. (This is the exact spot spec.md §5.1 would add a `geocode_status != 'NEEDS_HILT'` exclusion — not built yet.)
4. `solve_route_order` (`services/tsp.py`) — haversine distance matrix, OR-Tools `RoutingModel`, `PATH_CHEAPEST_ARC` + `GUIDED_LOCAL_SEARCH`, time-boxed by stop count (1s/3s/5s at ≤10/≤50/>50 stops).
5. Assign remaining sequence slots, geohash-locality clusters (`geohash_group_id = f"{drsNo}_{geohash_locality}"`), write back via `write_group_assignments` (BQ UPDATE by `sorting_id`) then `upsert_routing_from_sorting` (Firestore, routing fields only — deliberately excludes geocode-provenance fields, a merge).

## 7. Cross-cutting concerns as they exist today

- **Auth**: infra-level only (Cloud Run IAM, §2). No identity/role/tenant is extracted inside the app — every request is anonymous from the app's point of view once past the IAM gate.
- **CORS**: `allow_origins=["*"]`, `allow_methods=["*"]`, `allow_headers=["*"]` (`main.py`) — irrelevant for pure service-to-service IAM-gated calls, but not defense-in-depth.
- **Config**: static values (project IDs, table names, API key, geohash lengths, cache TTLs) are plain `os.environ[...]` reads in `config.py`, required at import time (deploy fails fast if unset). No dynamic values existed before this session.
- **Error handling convention**: routers return `{"status": "ok"|"partial_success"|"error", ...}` dicts, `JSONResponse(status_code=...)` on failure — consistent across both routers.
- **Logging**: plain `logging.basicConfig` to stdout, free-text messages, no structured fields, no correlation/trace IDs threading a request across `save.py` → `geocache.py`/`master_waypoint.py` → BigQuery/Firestore.
- **Observability**: no OpenTelemetry, no Prometheus. The only signal is `drs_cache_metrics` — a bespoke, write-only Firestore analytics log, not wired to any dashboard/alert that this repo can see.
- **Testing**: `unittest`, real CI enforcement, but narrow — see §10.

## 8. New this session: feature flags & master-waypoint tier

### 8.1 `app/db/feature_flags.py`

Dynamic config store: one Firestore doc per flag under `app_config`, id = flag name. Read path mirrors `geocache.py`'s existing `_verified_entries()` snapshot pattern — load once, cache in-process for `FEATURE_FLAG_SNAPSHOT_TTL_SECONDS` (45s default, `config.py`), reload after. A read failure keeps the last-known-good snapshot rather than defaulting everything off mid-flight; a *cold-start* failure (no prior snapshot) defaults to off — never raises out to a caller.

```
app_config/{flag_name}
{
  enabled: bool,
  version: int,
  gates: [<spec section ids>],
  ...flag-specific values, e.g. thresholds_km: {suggest, medium, block},
  created_at / updated_at: timestamps
}
```

Public surface: `is_enabled(name, default=False)`, `get_flag(name)`, `get_value(name, key, default=None)`, `invalidate_snapshot()` (test/manual reset), `seed_default_flags()` — idempotent, creates any of five known flags (`master_waypoint_v2`, `geocoding_ambiguity_v2`, `outlier_trap_v2`, `eod_ingestion_v2`, `bqml_embeddings_eval`) that don't already exist, always disabled, never touches an existing doc. Only `master_waypoint_v2` has a live caller today; the other four are defined but unused, waiting on spec.md §5.1/§5.2/§5.4/§5.5.

### 8.2 `app/db/master_waypoint.py`

Exact-match (no fuzzy tier, deliberately — a false positive here sends a driver to the wrong building with zero API call to catch it) lookup against a `verified: True` entry in `master_waypoints`, keyed by the same `normalize_address()` `geocache.py` already uses (reused, not reimplemented). Three operations:
- `lookup(alias_or_address)` — read-only, returns `{latitude, longitude, formatted_address, source: "master_waypoint"}` or `None`. Any Firestore error also returns `None` — never blocks the caller falling through to `geocache`.
- `seed(alias, lat, lon, ...)` — ops entry point for a manually-confirmed anchor. Refuses to overwrite an existing `verified: True` doc.
- `record_confirmation(alias, corrected_lat, corrected_lon, consignment_id)` — the sole other write path. Tracks distinct `consignmentId`s confirming the same rounded (5dp) coordinate; on the 3rd distinct confirmation, promotes to `verified: True`. **Not wired to a live caller yet** — this is what spec.md §5.4's (unbuilt) EOD endpoint will call.

### 8.3 Integration point

`app/services/save.py::_geocode()` gained one new block at the top, gated by `feature_flags.is_enabled("master_waypoint_v2")`:

```python
if feature_flags.is_enabled("master_waypoint_v2"):
    waypoint_hit = master_waypoint.lookup(lookup_address or address)  # wrapped in try/except
    if waypoint_hit:
        return waypoint_hit, None, None, "master_waypoint_hit"
# falls through to the existing geocache -> Places API logic, unmodified
```

Flag off (the shipped default) → this block never executes, `_geocode()` is byte-for-byte what it was before this session. The new `"master_waypoint_hit"` outcome also threads through `save_consignments_pipeline`'s metrics dict (`masterWaypointHits`, alongside `exactHits`/`fuzzyHits`/etc.) and `cache_metrics.py`'s `hitRate` calculation.

## 9. Known divergence: `main` has parallel, unmerged work

While this branch was in progress, `main` picked up real production work (merge of a `dev` branch, authored by Akshay) that this branch's PR (#3) has not merged and currently conflicts with:

- `app/db/drs_memo.py` — a **per-DRS** address memo (`drs_address_memo` collection), reusing a pin for same-place consignments *within one DRS*, with source-priority tiers (`exec_corrected` > `exec_accepted` > `global_cache` > `api`).
- `app/services/address_match.py` — the phone/door-number/fuzzy matching logic feeding it.
- A rewritten `_geocode()` on `main` with a 4-tier waterfall and a different signature than what's described in §5 above.
- Commit `af5c6e7` — a real incident fix (case `CCU501619425`): the sort pipeline was reverting manually-corrected pins; fixed by splitting `_base_routing_doc` (sort-owned) from `_save_routing_doc` (save-owned) in `firestore.py` so sort can no longer touch location fields.

This document describes **this branch's** architecture as the coherent thing it is; it does not attempt to describe `main`'s version of `_geocode()` in detail. See PR #3's discussion and `.specify/ARCHITECTURE_INVESTIGATION.md` §1/§6 for the reconciliation question — cross-DRS/promotion-based (`master_waypoint`) vs. per-DRS/executive-correction-based (`drs_memo`) is a real design decision, not yet resolved as of this document.

## 10. Tech debt inventory

Full detail and reasoning in `.specify/spec.md` §8; summarized here for architectural context:

| ID | Item | Where | Architectural implication |
|---|---|---|---|
| TD-1 | Pydantic request models have no field descriptions | `app/schemas.py` | Thin input contract — `SaveConsignmentsRequest`/`RunSortingRequest` barely constrain shape; also weakens FastAPI's auto-generated `/docs` |
| TD-2 | `GEOCODE_URL` defined, never called | `app/config.py:12` | Dead config referencing the classic Geocoding API; only `PLACES_SEARCH_URL` is actually used |
| TD-3 | `get_cached_geocode()` has no callers in this repo | `app/db/geocache.py:268` | Possible dead code, or an undocumented external dependency — unconfirmed either way |
| TD-4 | Unparameterized SQL string interpolation | `app/db/bigquery.py:199-235`, `write_group_assignments` | The one query in the file not using `bigquery.ScalarQueryParameter`/`ArrayQueryParameter`; `starting_address` is free text that can break the query syntactically (an apostrophe) today, and is an injection-shaped pattern architecturally inconsistent with the rest of the module |
| TD-5 | `location_type` threaded through every layer, always `None` at source | `geocoding.py:154` → `schemas`/`save.py`/`bigquery.py`/`firestore.py` | A vestigial column carried through the full stack (BQ + Firestore + the MERGE SQL) with no real data — a port artifact from a prior Geocoding-API-based implementation |
| TD-6 | Zero test coverage on `sorting.py`, `tsp.py`, BigQuery writes | `tests/` only covers `geocache`/`address`/`cache_metrics`/(this session's) `feature_flags`/`master_waypoint` | The route-solving and BQ-write path — arguably the highest-consequence code (wrong route, wrong data written) — is the least tested part of the system |

**Not yet in the tracked inventory, surfaced by this document's §7 review:** the auth/authz gap (app has no identity model once past Cloud Run IAM) and the total absence of tenancy in the data model are architectural gaps, not line-level tech debt — tracked instead in `.specify/ARCHITECTURE_INVESTIGATION.md` §2/§4, since they're investigation objectives rather than a fix-in-place item.

## 11. Testing architecture

- Framework: stdlib `unittest`, run via `python -m unittest discover -s tests -v`, enforced in CI on every PR/push to `dev`/`main`.
- Style: pure-Python unit tests. No live GCP credentials required anywhere — `geocache`/`address` tests either import the real normalization functions (pure logic, safe to import) or mirror them inline; this session's `feature_flags`/`master_waypoint` tests go further and provide full fake Firestore doubles (`_FakeClient`/`_FakeCollection`/`_FakeDocRef`/`_FakeQuery`) supporting `.document()`/`.where()`/`.limit()`/`.stream()`/`.get()`/`.set(merge=...)` — good enough fidelity to catch a real bug during this session (a fake returning a live dict reference instead of a copy from `to_dict()`, masking a caching bug).
- Coverage gap: `sorting.py`, `tsp.py`, and the BigQuery write functions (TD-6) have zero tests — no fake BigQuery client pattern exists yet in this repo to extend from.
- No integration tests against real or emulated Firestore/BigQuery, no route-level `TestClient` tests in this branch (unconfirmed whether `main`'s `test_geocode_preview_and_bypass.py` established that pattern — check there before building a second one from scratch).
