# Route Optimization

A single FastAPI service running on Cloud Run. It takes delivery consignments,
finds out where they actually are on the map, and then works out the best order
for a driver to deliver them.

It replaces the two older Cloud Functions (`save_consignments` and
`run_sorting`) with one service and two HTTP endpoints.

---

## What it does, in plain words

There are **two jobs**. They run separately, and the order matters.

### Job 1 — Save (`POST /save-consignments`)

**Question it answers: "where is this parcel going?"**

You give it a list of consignment IDs. For each one it:

1. Reads the consignment document from Firestore (`consignments`) to get the
   receiver name and address.
2. Cleans the address into one single line, and joins name + address into one
   search string.
3. Looks that address up in the **geocode cache** (Firestore `geocode_cache`).
   If the same (or a very similar) address was geocoded before *and* an ops
   person marked it `verified`, it reuses that answer and skips the API call.
   Before and after this step it checks the **DRS address memo** — pins
   already resolved for the same receiver/place earlier in this DRS (see
   [DRS address memo](#drs-address-memo)).
4. On a miss everywhere, calls the **Google Places API** to get latitude, longitude,
   pincode, locality, area and so on. The new result is written back into the
   cache as `verified: false`, so it is stored but not yet trusted for reuse.
5. Turns the coordinates into geohashes at three precisions — exact (8),
   building (6) and locality (5). The locality geohash is what later groups
   nearby parcels into clusters.
6. Writes the row into BigQuery (`consignments_routing`) with a `MERGE`:
   - **New consignment** → a new row is inserted, with a generated `sorting_id`
     and `geohash_group_id = 'UNASSIGNED'`.
   - **Existing consignment** → only the address and geocode columns are
     updated. The sequence and group columns are deliberately **not** touched,
     so re-saving a parcel does not wipe out a route that was already built.
7. Copies the resulting rows back into Firestore (`consignments_routing`) so the
   app can read them.

At this point every parcel has a location, but **no delivery order yet**. The
`starting_*` columns are filled with placeholders (`PENDING_OPTIMIZATION`, 0, 0)
because the driver's start point often does not exist yet when saving happens.

If a consignment has no usable address, or Google cannot resolve it, the row is
still written with `geocode_status = 'failed'` and the reason in
`geocode_error`, and the consignment is listed in the response `failures`.

### Job 2 — Sort (`POST /run-sorting`)

**Question it answers: "in what order should the driver deliver them?"**

You give it one DRS number. For that DRS it:

1. Reads the driver's starting point (hub/depot) from Firestore
   `drs_starting_point`. If it is missing, the first stop is used as the hub and
   a warning is logged.
2. Reads **every active parcel** for that DRS from BigQuery. This query joins
   two tables:
   - `consignments_routing` — our table: coordinates, geohashes, current
     sequence numbers.
   - `consignments_structured` — the delivery-status export: `status` and
     `statusCode`.
3. Splits the parcels into two piles using that status:
   - **Already delivered** (`statusCode = 'DE'`) → these are *frozen*. They keep
     the sequence number they already have, and that number is marked as taken.
   - **Still pending** → these go into the optimizer.
4. Solves the order with **OR-Tools**, as an open-path TSP: start at the hub,
   visit every pending stop once, and do not come back. Distance is
   straight-line (haversine), not road distance. The solver is time-capped at
   1 second (≤10 stops), 3 seconds (≤50) or 5 seconds (more).
5. Drops the solved stops into the sequence numbers that are still free, in
   order.
6. For each stop it also writes:
   - `geohash_group_id` = `<drsNo>_<locality geohash>` — the cluster the stop
     belongs to.
   - `planned_sequence_order` — position in the whole route.
   - `planned_inside_cluster_sequence` — position inside its own cluster.
   - `actual_*` copies of both. These start identical to the plan and are meant
     to drift later as the driver actually works (e.g. skips a stop).
7. Writes all of that back to BigQuery, then syncs the updated rows to Firestore
   `consignments_routing`.

#### Why the delivery status matters — an example

A DRS has 10 stops. The driver has finished stops 1, 2 and 3. Two new parcels
now arrive, so sorting is run again.

- **Without** the status join, the optimizer would renumber all 10 stops from 1.
  Parcels the driver already delivered would move around and the route on his
  phone would stop matching reality.
- **With** it, stops 1–3 are recognised as delivered, their numbers are locked,
  and only the remaining parcels are re-optimized into the free slots 4–10.

That is the only reason `consignments_structured` is read. Note it is an
**inner join**: a parcel present in `consignments_routing` but missing from
`consignments_structured` is silently skipped and never sorted.

### Typical order of operations

```
save-consignments   ->  parcel has coordinates, no order yet
(driver start point exists in drs_starting_point)
run-sorting         ->  parcel has cluster + sequence number
run-sorting again   ->  delivered stops stay put, the rest are re-optimized
```

---

## DRS address memo

Executives verify `geocode_cache` entries only at the end of a DRS, so during
the day a second parcel to the same receiver misses the cache and would call
Places again (and may get a slightly different pin). The memo closes that gap.

**Lookup order** (`_geocode` in `app/services/save.py`):

| Step | Source | Notes |
|------|--------|-------|
| 0 | `confirmedLocations` on save | As before; the confirmed pin is also written to the memo |
| 1 | Memo — executive-**corrected** entries | A correction made in this DRS today beats everything |
| 2 | Verified `geocode_cache` | Addresses verified at the end of earlier DRSs |
| 3 | Memo — any entry | Same receiver/place resolved earlier in this DRS |
| 4 | Places API | Result is saved to `geocode_cache` and the memo |

**When two consignments count as the same place** (`app/services/address_match.py`),
using `receiver.fullAddress` (falls back to `receiver.address`),
`receiver.phone` and `receiver.addressComponent`:

1. Different pincode → never.
2. Same `receiver.phone` → yes, unless both have door numbers and they differ.
   Only `receiver.phone` is used; numbers inside the address text are ignored.
3. Either address road-level (no door number, fewer than 3 distinctive words,
   e.g. OCR text "Kodathi Village Main Road, Kodathi Gate, Bangalore …") → no
   text match.
4. Door numbers identical, and the texts are equal, one contains the other
   (≥ 90 % of the shorter one's words), or `token_sort_ratio` ≥ 90.

Thresholds are in `app/config.py` (`DRS_MEMO_*`).

**End-of-DRS verification.** When a memo pin is reused under a new spelling,
that spelling gets its own unverified `geocode_cache` entry with the same pin
and a `drs_memo_group` field. `app.db.geocache.verify_cache_group(group, by)`
verifies every spelling in a group at once, so all of them are served from
the cache the next day.

**Scan preview.** `POST /geocode/preview` accepts optional `drsId` and
`consignmentId`. With `consignmentId` the backend reads phone / fullAddress
from the consignment itself; without either, the memo is skipped.

**One-time setup.** Enable a Firestore TTL policy on collection
`drs_address_memo`, field `expires_at` (entries live `DRS_MEMO_TTL_DAYS` = 2
days).

---

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Liveness check |
| `POST` | `/save-consignments` | Geocode and persist consignments |
| `POST` | `/run-sorting` | Optimize route order for a DRS |
| `GET` | `/docs` | OpenAPI / Swagger UI |
| `GET` | `/redoc` | ReDoc |

### Save consignments

```http
POST /save-consignments
Content-Type: application/json

{ "consignmentIds": ["id1", "id2"] }
```

A single id is also accepted: `{ "consignmentId": "id1" }`.

Success (`200`):

```json
{
  "status": "ok",
  "saved": 2,
  "failed": 0,
  "failures": [],
  "started_at": "...",
  "finished_at": "...",
  "duration_seconds": 1.23
}
```

`status` is `partial_success` when some ids fail geocoding or are missing.
Failures are listed as `{ "consignmentId", "reason" }`. Missing body returns
`400`; unexpected errors return `500`.

### Run sorting

```http
POST /run-sorting
Content-Type: application/json

{ "drsno": "DRS123" }
```

Query param `?drsno=` is also accepted.

Success (`200`):

```json
{
  "status": "ok",
  "drsNo": "DRS123",
  "optimized_count": 40,
  "starting_time": "...",
  "ending_time": "...",
  "duration_seconds": 4.56
}
```

`optimized_count` is the number of rows that were given a new sequence — that
is, pending stops only. Delivered stops are counted as part of the route length
but are not re-numbered, so this figure is usually smaller than the DRS size.

If there are no active BigQuery rows for that DRS, `optimized_count` is `0` and
a `message` is returned.

---

## Data stores

| Store | Read / Write | Use |
|-------|--------------|-----|
| Firestore `consignments` | read | Source consignment documents (receiver name and address) |
| Firestore `geocode_cache` | read + write | Previously geocoded addresses. Only entries flagged `verified: true` are reused; new entries are stored as `verified: false` |
| Firestore `drs_address_memo` | read + write | One document per DRS: pins already resolved in that DRS, reused for later same-place consignments. Needs a TTL policy on `expires_at` |
| Firestore `drs_cache_metrics` | write | One aggregate cache summary per DRS save run: exact/fuzzy hits, misses, API calls, and hit rate. This is written after the routing save and never blocks it. |
| Firestore `drs_starting_point` | read | Hub/depot lat/lon and address, keyed by DRS number |
| Firestore `consignments_routing` | write | Mirror of the BigQuery row, written after both pipelines, so the app can read it |
| BigQuery `consignments_routing` (`BQ_TABLE`) | read + write | System of record for geocode fields, clusters and sequence |
| BigQuery `consignments_structured` (`BQ_STRUCTURED_TABLE`) | read only | Delivery status (`status`, `statusCode`). Used only to protect already-delivered stops during re-sorting |

Save writes geocode data and leaves grouping as `UNASSIGNED` with `starting_*`
placeholders. Sort fills `geohash_group_id`, the `planned_*` / `actual_*`
sequence fields, and the real starting point.

The two Firestore syncs differ slightly: after save, the geocode provenance
fields (`formatted_address`, `place_id`, `geocode_status`, …) are included;
after sorting only the routing fields are written, so the provenance from save
is left untouched. Both writes are merges.

---

## Layout

```
app/
  main.py                 FastAPI app, CORS, /health
  config.py               Environment variables
  schemas.py              Request models
  routers/                HTTP handlers
    save.py
    sorting.py
  services/               Business logic
    address.py            Address cleanup
    geocoding.py          Google Places lookup + flag derivation
    save.py               Save pipeline
    tsp.py                OR-Tools open-path TSP
    sorting.py            Sort pipeline
  db/                     Data access
    clients.py            Shared BQ / Firestore clients
    firestore.py          Firestore reads/writes
    geocache.py           Address-scoped geocode cache (exact + fuzzy match)
    bigquery.py           BigQuery reads/writes
Procfile                  Cloud Run buildpacks start command
.python-version           Python 3.13 (ubuntu2404 builder)
requirements.txt
```

---

## Environment variables

All of these are read with `os.environ[...]` and have **no defaults** — if one
is missing the app fails to start.

| Variable | Required | Example | Description |
|----------|----------|---------|-------------|
| `GOOGLE_MAPS_API_KEY` | Yes | — | Google Places / Geocoding API key |
| `BQ_PROJECT` | Yes | `prj-dev-hermes` | GCP project for BigQuery |
| `BQ_DATASET` | Yes | `Hermes_Exports` | BigQuery dataset holding both tables |
| `BQ_TABLE` | Yes | `consignments_routing` | Routing table (read + write) |
| `BQ_STRUCTURED_TABLE` | Yes | `consignments_structured` | Delivery-status export (read only) |
| `FIRESTORE_PROJECT` | Yes | `prj-dev-hermes` | Firestore project |
| `PORT` | Cloud Run | `8080` | HTTP port |

Both BigQuery tables are expected in the same project and dataset.

The runtime service account needs BigQuery Data Editor on `BQ_TABLE` (or the
equivalent query/update rights), read access on `BQ_STRUCTURED_TABLE`, and
Firestore read/write on the collections listed above.

Tuning constants that are **not** environment variables live in `app/config.py`:
geohash lengths, batch sizes (500), the fuzzy-cache threshold (85) and the cache
snapshot TTL (300 s).

---

## Local run

Python 3.11+ locally (Cloud Run buildpacks use 3.13). Application Default
Credentials (for example `gcloud auth application-default login`).

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

$env:GOOGLE_MAPS_API_KEY  = "YOUR_KEY"
$env:BQ_PROJECT           = "prj-dev-hermes"
$env:BQ_DATASET           = "Hermes_Exports"
$env:BQ_TABLE             = "consignments_routing"
$env:BQ_STRUCTURED_TABLE  = "consignments_structured"
$env:FIRESTORE_PROJECT    = "prj-dev-hermes"

uvicorn app.main:app --reload --port 8080
```

Then open http://localhost:8080/docs or use the curl / Postman steps below (skip
the `Authorization` header for local).

---

## Environments and deployment

The repository has two isolated environments, and the branch decides which one
you are deploying to:

| Branch | Environment | Cloud Run service |
|---|---|---|
| `dev` | development | `route-optimization-dev` |
| `main` | production | `route-optimization` |

Pull requests run the test suite only. Merging is what deploys.

> **Full details are in [`DEPLOYMENT.md`](./DEPLOYMENT.md)** — every GitHub
> secret and variable and what it is for, how the OIDC login to GCP works, the
> deploy command flag by flag, one-time GCP setup, the step-by-step flow for
> shipping a bug fix, rollback, and troubleshooting.

Two things to know before you change anything:

- **The app's environment variables are set by the workflow files**, not the
  Cloud Run console. Editing them in the console lasts only until the next
  deploy. To change a table or dataset name, edit the workflow YAML.
- **`GOOGLE_MAPS_API_KEY` comes from Secret Manager**, secret name
  `google-maps-api-key`. It is never in git.

---

## Cloud Run service reference

### Service

| Setting | Value |
|---------|--------|
| Service name | `route-optimization` |
| Region | `asia-south1` |
| URL | `https://route-optimization-567483485783.asia-south1.run.app` |
| Authentication | Require authentication (IAM) |
| Billing | Request-based |
| Ingress | All |
| Timeout | 300 seconds |
| Build type | Cloud Run source deploy (Dockerfile) |
| Branch trigger | GitHub Actions `main` workflow |
| Build context | `/` |
| Function target | leave empty |

The repo root has a `Dockerfile` used by the deployment workflow.

### Environment variables on the service

These are applied by the deploy workflow on every rollout, so the workflow YAML
is the place to change them:

| Variable | Set by | Example |
|----------|--------|---------|
| `BQ_PROJECT` | workflow (from the project-ID variable) | `prj-dev-hermes` |
| `FIRESTORE_PROJECT` | workflow (from the project-ID variable) | `prj-dev-hermes` |
| `BQ_DATASET` | workflow | `Hermes_Exports` |
| `BQ_TABLE` | workflow | `consignments_routing` |
| `BQ_STRUCTURED_TABLE` | workflow | `consignments_structured` |
| `GOOGLE_MAPS_API_KEY` | Secret Manager (`google-maps-api-key:latest`) | — |

Memory, CPU and concurrency are *not* set by the workflow, so console changes to
those survive a deploy. Raise timeout and memory if DRS batches are large;
sorting time grows with stop count (OR-Tools search is capped at 1–5 seconds
internally).

### Shipping a change

The short version — the full walkthrough is in
[`DEPLOYMENT.md`](./DEPLOYMENT.md):

```
branch off dev  ->  PR into dev   ->  merge  ->  development deploys
verify on development
PR dev -> main  ->  merge  ->  production deploys
```

Never commit directly to `dev` or `main`, and never put a secret value in git.

---

## Testing

Set the base URL. For local use `http://localhost:8080`. For Cloud Run:

```powershell
$base = "https://route-optimization-567483485783.asia-south1.run.app"
```

The service requires IAM authentication, so every request needs a Bearer token:

```powershell
$token = gcloud auth print-identity-token
```

The token expires in about an hour. Re-run `print-identity-token` if you get
`401` or `403`.

On Windows PowerShell, do **not** put JSON inline in `curl.exe -d '...'`.
PowerShell strips quotes and FastAPI returns `json_invalid`. Write the body to a
file instead.

### curl — health

```powershell
curl.exe -sS -X GET "$base/health" `
  -H "Authorization: Bearer $token"
```

Expected: `{"status":"ok"}`

### curl — save consignments

```powershell
[System.IO.File]::WriteAllText("$pwd\save-body.json", '{"consignmentIds":["HRD363615390"]}')

curl.exe -g -sS -m 300 -X POST "$base/save-consignments" `
  -H "Authorization: Bearer $token" `
  -H "Content-Type: application/json" `
  --data-binary "@save-body.json"
```

Single consignment:

```powershell
[System.IO.File]::WriteAllText("$pwd\save-body.json", '{"consignmentId":"HRD363615390"}')
```

Several consignments:

```powershell
[System.IO.File]::WriteAllText("$pwd\save-body.json", '{"consignmentIds":["HRD363615390","HRD363615391"]}')
```

### curl — run sorting

```powershell
[System.IO.File]::WriteAllText("$pwd\sort-body.json", '{"drsno":"DRS123"}')

curl.exe -g -sS -m 300 -X POST "$base/run-sorting" `
  -H "Authorization: Bearer $token" `
  -H "Content-Type: application/json" `
  --data-binary "@sort-body.json"
```

Or pass DRS as a query param:

```powershell
curl.exe -g -sS -m 300 -X POST "$base/run-sorting?drsno=DRS123" `
  -H "Authorization: Bearer $token"
```

### curl — Linux / macOS / Git Bash

Quotes work normally here. Still use `-g` so `[...]` is not treated as a URL range.

```bash
BASE="https://route-optimization-567483485783.asia-south1.run.app"
TOKEN="$(gcloud auth print-identity-token)"

curl -sS -X GET "$BASE/health" \
  -H "Authorization: Bearer $TOKEN"

curl -g -sS -m 300 -X POST "$BASE/save-consignments" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"consignmentIds":["HRD363615390"]}'

curl -g -sS -m 300 -X POST "$BASE/run-sorting" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"drsno":"DRS123"}'
```

Local (no token):

```bash
curl -sS http://localhost:8080/health
curl -g -sS -X POST http://localhost:8080/save-consignments \
  -H "Content-Type: application/json" \
  -d '{"consignmentIds":["HRD363615390"]}'
curl -g -sS -X POST http://localhost:8080/run-sorting \
  -H "Content-Type: application/json" \
  -d '{"drsno":"DRS123"}'
```

### Postman

1. Create a collection, e.g. **Route Optimization**.
2. Add a collection variable `baseUrl` = `https://route-optimization-567483485783.asia-south1.run.app` (or `http://localhost:8080`).
3. **Auth (Cloud Run only)**
   - In the collection **Authorization** tab, type **Bearer Token**.
   - Token value: run `gcloud auth print-identity-token` in a terminal and paste the output.
   - Child requests can use **Inherit auth from parent**.
   - For local, set Authorization to **No Auth**.
4. Create three requests:

**Health**

| Field | Value |
|-------|--------|
| Method | `GET` |
| URL | `{{baseUrl}}/health` |

**Save consignments**

| Field | Value |
|-------|--------|
| Method | `POST` |
| URL | `{{baseUrl}}/save-consignments` |
| Headers | `Content-Type: application/json` |
| Body | **raw** → **JSON** |

```json
{
  "consignmentIds": ["HRD363615390"]
}
```

**Run sorting**

| Field | Value |
|-------|--------|
| Method | `POST` |
| URL | `{{baseUrl}}/run-sorting` |
| Headers | `Content-Type: application/json` |
| Body | **raw** → **JSON** |

```json
{
  "drsno": "DRS123"
}
```

5. Send. Typical results:
   - `200` with `"status": "ok"` or `"partial_success"` — request reached the app.
   - `401` / `403` — missing or expired token, or your user lacks `roles/run.invoker`.
   - `400` — missing `consignmentIds` / `drsno`.
   - `500` with a BigQuery `Name ... not found inside T` — the target table is missing a column (for example `geocode_address`).

Save can take a while (geocoding). In Postman set **Settings → Request timeout**
high enough (e.g. 300000 ms), matching the Cloud Run timeout of 300 seconds.

---

## Known rough edges

Worth knowing before you debug something surprising:

- **The status join is an inner join.** A parcel in `consignments_routing` with
  no matching row in `consignments_structured` is dropped from sorting entirely
  and never gets a sequence number.
- **One row per consignment is assumed.** If `consignments_structured` ever
  holds more than one row for the same `(drsNo, consignmentId)`, the join will
  fan out and the same parcel will be sorted more than once.
- **Distances are straight-line, not road distance.** Haversine ignores
  one-ways, rivers and flyovers, so the solved order is a good approximation and
  not a driving-time optimum.
- **A delivered parcel with no existing sequence number** cannot hold a slot; it
  is logged as a warning and the numbering can end up not landing exactly on
  `1..N`.
- **Nothing in this service sets `verified: true`** on a geocode cache entry.
  That is an ops decision made outside the app, and until it is made the cached
  address will not be reused.
