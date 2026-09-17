# Route Optimization

Single FastAPI service on Cloud Run that geocodes consignments and builds a delivery sequence for a DRS.

It replaces the previous two Cloud Functions (`save_consignments` and `run_sorting`) with one service and two HTTP endpoints.

## What it does

1. **Save** — load consignments from Firestore, geocode receiver addresses with Google Maps, upsert rows into BigQuery, then sync `consignments_routing` back to Firestore.
2. **Sort** — load active consignments for a DRS, solve an open-path TSP from the DRS starting point with OR-Tools, write sequence/cluster fields to BigQuery, then sync Firestore.

Typical order: save consignments first, then run sorting when the driver/hub starting point exists.

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

`status` is `partial_success` when some ids fail geocoding or are missing. Failures are listed as `{ "consignmentId", "reason" }`. Missing body returns `400`; unexpected errors return `500`.

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

If there are no active BigQuery rows for that DRS, `optimized_count` is `0` and a `message` is returned.

## Data stores

| Store | Use |
|-------|-----|
| Firestore `consignments` | Source consignment documents |
| Firestore `drs_starting_point` | Hub/depot lat/lon and address, keyed by DRS number |
| Firestore `consignments_routing` | Routing snapshot write-back (commented out in the pipelines) |
| BigQuery `consignments_routing_test` | System of record for geocode fields, sequence, and clusters |

Save writes geocode data and leaves grouping as `UNASSIGNED` with `starting_*` placeholders. Sort fills `geohash_group_id`, `planned_*` / `actual_*` sequence, and the real starting point.

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
    geocoding.py          Google Maps geocode
    save.py               Save pipeline
    tsp.py                OR-Tools open-path TSP
    sorting.py            Sort pipeline
  db/                     Data access
    clients.py            Shared BQ / Firestore clients
    firestore.py          Firestore reads/writes
    bigquery.py           BigQuery reads/writes
Procfile                  Cloud Run buildpacks start command
.python-version           Python 3.13 (ubuntu2404 builder)
requirements.txt
```

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GOOGLE_MAPS_API_KEY` | Yes (save) | — | Geocoding API key |
| `BQ_PROJECT` | No | `prj-dev-hermes` | GCP project for BigQuery |
| `BQ_DATASET` | No | `Hermes_Exports` | BigQuery dataset |
| `BQ_TABLE` | No | `consignments_routing_test` | BigQuery table |
| `FIRESTORE_PROJECT` | No | same as `BQ_PROJECT` | Firestore project |
| `PORT` | Cloud Run | `8080` | HTTP port |

The runtime service account needs BigQuery Data Editor (or equivalent query/update on the table) and Firestore read/write on the collections above.

## Local run

Python 3.11+ locally (Cloud Run buildpacks use 3.13). Application Default Credentials (for example `gcloud auth application-default login`).

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

$env:GOOGLE_MAPS_API_KEY = "YOUR_KEY"
$env:BQ_PROJECT = "prj-dev-hermes"
$env:BQ_DATASET = "Hermes_Exports"
$env:BQ_TABLE = "consignments_routing_test"
$env:FIRESTORE_PROJECT = "prj-dev-hermes"

uvicorn app.main:app --reload --port 8080
```

Then open http://localhost:8080/docs or use the curl / Postman steps below (skip the `Authorization` header for local).

## Environments and GitHub CI/CD

The repository has two isolated deployment environments. Pull requests and pushes
to either deployment branch run the test suite. GitHub Actions then deploys only
the matching branch:

| Branch | GitHub environment | GCP project | Cloud Run service |
|---|---|---|---|
| `dev` | `development` | `hermes-dev-508805` | `route-optimization-dev` |
| `main` | `production` | configured in GitHub | configured in GitHub |

The deployment workflows use GitHub OpenID Connect (OIDC), not a downloaded GCP
service-account key. Before the first deployment, configure these repository
secrets in **Settings → Secrets and variables → Actions**:

| Secret | Value |
|---|---|
| `GCP_DEV_WORKLOAD_IDENTITY_PROVIDER` | Full Workload Identity Provider resource name for `hermes-dev-508805` |
| `GCP_DEV_SERVICE_ACCOUNT` | Development deploy service-account email |
| `GCP_PROD_WORKLOAD_IDENTITY_PROVIDER` | Full production Workload Identity Provider resource name |
| `GCP_PROD_SERVICE_ACCOUNT` | Production deploy service-account email |

Add one repository variable: `GCP_PROD_PROJECT_ID`, containing the current
production GCP project ID. The production workflow deploys `route-optimization`
in `asia-south1` and preserves the existing Cloud Run environment variables and
secret mappings. The development workflow expects its Maps secret to be named
`google-maps-api-key` in `hermes-dev-508805`.

Each deploy service account needs permission to deploy Cloud Run, act as the
Cloud Run runtime service account, trigger source builds, and read the Maps
secret. In practice, grant the least-privilege equivalent of Cloud Run Admin,
Service Account User, Cloud Build Editor, Artifact Registry Writer, and Secret
Manager Secret Accessor. Protect the `main` branch with required pull-request
reviews before production deployment.

The existing Cloud Build trigger for `main` must be disabled before merging
these workflows, otherwise both Cloud Build and GitHub Actions will deploy
production from the same commit.

## Deploy to Cloud Run

Deploys are initiated by GitHub Actions. The action uses Cloud Run source deploys,
which build the repository and roll out a new revision. The service details below
describe the production target.

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

### Environment variables (Cloud Run console)

Set these once on the service (Containers → Variables). Later GitHub deploys keep them; you do not pass them in a deploy command.

| Variable | Example |
|----------|---------|
| `BQ_PROJECT` | `prj-dev-hermes` |
| `BQ_DATASET` | `Hermes_Exports` |
| `BQ_TABLE` | `consignments_routing_test` |
| `FIRESTORE_PROJECT` | `prj-dev-hermes` |
| `GOOGLE_MAPS_API_KEY` | your key |

Prefer Secret Manager for the Maps key in production. Raise timeout and memory if DRS batches are large; sorting time grows with stop count (OR-Tools search is capped at 1–5 seconds internally).

### How the team edits and deploys

Pushes to **`dev`** deploy development. A PR into `dev` or `main` only runs CI;
it does not deploy. A push to **`main`** deploys production.

1. Clone (or pull latest `main`):

```powershell
git clone git@github.com:Solsten-Data-Consulting-Pvt-Ltd/Route-Optimization.git
cd Route-Optimization
git checkout main
git pull origin main
```

2. Create a branch. Do not commit feature work straight on `main`.

```powershell
git checkout -b feat/short-description
```

3. Edit, run locally (see **Local run**), then commit and push the branch:

```powershell
git add .
git commit -m "Describe why this change exists."
git push -u origin HEAD
```

4. Open a pull request into **`dev`** on GitHub. Review, then merge to deploy to
development. Validate the development service before creating a PR from `dev`
into **`main`**.

5. Review the workflow run in GitHub Actions or the Cloud Run service revisions.
When the revision is ready, check:

- `https://route-optimization-567483485783.asia-south1.run.app/health`
- `.../save-consignments`
- `.../run-sorting`

Do **not** change environment values or secret values in git. Configure them in
the GitHub environment and Secret Manager as described above.

## Testing

Set the base URL. For local use `http://localhost:8080`. For Cloud Run:

```powershell
$base = "https://route-optimization-567483485783.asia-south1.run.app"
```

The service requires IAM authentication, so every request needs a Bearer token:

```powershell
$token = gcloud auth print-identity-token
```

The token expires in about an hour. Re-run `print-identity-token` if you get `401` or `403`.

On Windows PowerShell, do **not** put JSON inline in `curl.exe -d '...'`. PowerShell strips quotes and FastAPI returns `json_invalid`. Write the body to a file instead.

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

Save can take a while (geocoding). In Postman set **Settings → Request timeout** high enough (e.g. 300000 ms), matching the Cloud Run timeout of 300 seconds.
