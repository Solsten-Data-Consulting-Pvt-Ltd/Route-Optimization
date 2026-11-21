<!--
Sync Impact Report
- Version change: template -> 1.0.0 (initial ratification)
- Principles added: I-VII (all new)
- Sections added: Technology & Runtime Constraints, Development Workflow & Quality Gates, Governance
- Templates reviewed: plan-template.md (Constitution Check reads this file - OK),
  spec-template.md (OK), tasks-template.md (OK)
- Follow-ups: .python-version (3.13) and README "Local run"/"Layout" mention 3.13,
  but the Dockerfile and CI run 3.11 - align in a separate change.
-->

# Route Optimization Constitution

This service geocodes delivery consignments (`POST /save-consignments`) and orders
them into a driver route (`POST /run-sorting`). Drivers and operations staff act on
its output in real time, so these principles put route stability and data integrity
ahead of everything else.

## Core Principles

### I. Delivered Stops Are Frozen (NON-NEGOTIABLE)

- A stop whose delivery status is delivered (`statusCode = 'DE'` in
  `consignments_structured`) MUST keep its existing sequence number on every re-sort.
  Only pending stops are re-optimized, and only into the sequence slots still free.
- Save MUST NOT overwrite `geohash_group_id`, `planned_*` or `actual_*` sequence
  columns of an existing consignment. Re-saving updates address/geocode columns only.
- Any change to the sort or save pipeline MUST include a test proving these two
  guarantees still hold.

**Rationale**: the route on the driver's phone must match what already happened.
Renumbering completed stops breaks the driver's workflow.

### II. Trusted Geocodes Only

- A new Places API result MUST be written to `geocode_cache` as `verified: false`.
  Only entries with `verified: true` are reused from the cache.
- The geocode lookup order in `app/services/save.py` (confirmed location -> memo
  correction -> verified cache -> memo -> Places API) is a contract. Changing it
  needs a spec that says why.
- Same-place matching rules and thresholds live in `app/services/address_match.py`
  and `app/config.py` (`DRS_MEMO_*`). Changing a threshold requires tests with real
  address examples, including negative cases (different pincode, different door
  number).

**Rationale**: one wrong pin that gets reused sends every later parcel to the wrong
place.

### III. Fail Soft Per Consignment, Never Silently

- A consignment that cannot be geocoded MUST still be written, with
  `geocode_status = 'failed'` and a reason in `geocode_error`, and MUST appear in the
  response `failures`. One bad consignment MUST NOT fail the whole batch.
- Auxiliary writes (such as `drs_cache_metrics`) MUST NOT block or fail the main
  save or sort.
- Known silent behaviour (for example the inner join to `consignments_structured`,
  which drops rows missing from it) MUST be documented in the README. Adding new
  silent behaviour needs explicit sign-off in the spec.

### IV. BigQuery Is the System of Record

- BigQuery `consignments_routing` holds the authoritative data. Firestore
  `consignments_routing` is a merge-only mirror written after BigQuery succeeds.
- Writes MUST be idempotent (`MERGE` or merge writes), so re-running save or sort on
  the same input is safe.
- Reads and cleanup MUST be scoped to the DRS or consignments in the request, never
  table-wide.

### V. Test-Backed Changes

- Every behaviour change ships with `unittest` tests under `tests/` that pass with
  `python -m unittest discover -s tests -v`, which is what CI runs.
- Tests MUST NOT call live Google, BigQuery or Firestore. Use fakes or mocks.
  Scratch scripts in `scratch/` are not tests.
- Bug fixes start with a test that reproduces the bug.

### VI. Stable API Contracts

- Request and response shapes of `/save-consignments`, `/run-sorting`,
  `/geocode/preview` and `/health` are consumed by other apps. Changes MUST be
  additive (new optional fields). A breaking change needs a spec that names the
  affected consumers and a migration plan.
- Every endpoint or field change MUST update the README Endpoints section in the
  same PR.

### VII. Simplicity and Layering

- Keep the existing layers: `routers/` handle HTTP only, `services/` hold business
  logic, and `db/` does data access. Services must not build HTTP responses, and
  routers must not query stores directly.
- Add a dependency only if the spec justifies it. `requirements.txt` stays minimal,
  and new pins need a reason.
- Tuning constants go in `app/config.py`. Environment and deployment settings go in
  the workflow YAML, not the Cloud Run console.

## Technology & Runtime Constraints

- **Runtime**: Python 3.11 (the `Dockerfile` base image and CI), FastAPI + uvicorn,
  deployed to Cloud Run in `asia-south1` from the `Dockerfile`.
- **Optimizer**: OR-Tools open-path TSP with haversine distance. Solver time caps
  (1 s / 3 s / 5 s by stop count) keep the request well under the 300 s Cloud Run
  timeout. Changes MUST keep the total sort request within that timeout.
- **Secrets**: `GOOGLE_MAPS_API_KEY` comes only from Secret Manager. No secrets,
  keys or `env-vars.yaml` in git.
- **Configuration**: required environment variables have no defaults, and the app
  fails fast at startup if one is missing. Keep that behaviour.

## Development Workflow & Quality Gates

- **Branches**: work on a feature branch off `dev` and open a PR to `dev`. Merging
  to `dev` deploys to development, and merging `dev` into `main` deploys to
  production. Never commit directly to `main`.
- **Spec-driven work**: for anything larger than a small bug fix, use
  `/speckit-specify` -> `/speckit-plan` -> `/speckit-tasks` -> `/speckit-implement`.
  Specs live in `specs/NNN-feature/` and are committed with the change.
- **Gates before merge**:
  1. CI passes.
  2. The plan's Constitution Check has no unjustified violations.
  3. README/DEPLOYMENT docs are updated for any behaviour, endpoint, env var or
     infrastructure change.
- **Data changes**: BigQuery schema or Firestore collection changes MUST describe the
  one-time setup (for example TTL policies or new columns) in the spec and in
  `DEPLOYMENT.md`.

## Governance

- This constitution overrides other guidance for this repository. Plans and reviews
  MUST check compliance with it, and any exception MUST be recorded in the plan's
  Complexity Tracking table.
- Amendments are made by PR to this file, using semantic versioning. MAJOR means a
  principle is removed or redefined, MINOR means a principle or section is added,
  and PATCH means clarifications. Update the Sync Impact Report and dates with each
  amendment.
- Runtime and product details belong in `README.md` and `DEPLOYMENT.md`. This file
  holds only the rules.

**Version**: 1.0.0 | **Ratified**: 2026-09-26 | **Last Amended**: 2026-09-26
