# OSRM (self-hosted road distances) — phase-by-phase implementation and test plan (23 Nov 2026)

Owner: Akshay with Archana. Based on a read of both repos. No code changed yet.

## Goal and scope
Replace Route-Optimization's paid Google Routes "Compute Route Matrix" calls with our own OSRM server (open-source road routing on OpenStreetMap data), **keeping all routing logic unchanged** — only the source of the stop-to-stop road distances changes. Google stays as automatic fallback, switchable by a Firestore flag with no redeploy. 3PL's Mapbox calls (route line, delivery road distance, far-from-route check) are an optional later phase.

## What the code looks like today (relevant parts)

**Route-Optimization** (Python, Cloud Run `route-optimization`, asia-south1, deployed by GitHub Actions `deploy-dev.yml` / `deploy-prod.yml`, secret `GOOGLE_MAPS_API_KEY`)
- `app/services/road_matrix.py` — builds the distance table: `fetch_row()` calls Google (one origin, ≤25 destinations), `build_distance_matrix()` does cache → Google → estimate, with element cap, 429 retry, nearest-first ordering. Cache: Firestore `road_distance_cache` (geohash-7 cell pairs, 30 days).
- `app/services/sorting.py` — `_road_matrix()` calls `build_distance_matrix`; any failure → straight-line. `run_sorting_pipeline()` writes a `routing_runs` record per optimize (from_api, from_cache, estimated…).
- `app/db/feature_flags.py` — reads 3PL's `featureFlags` collection; today only boolean `enabled` (`roadDistanceRouting` is on in prod).
- `app/services/tsp.py` — OR-Tools solver, takes the matrix; unchanged by this work.
- `offline_eval/` — existing place for offline evaluation scripts.

**3PL-Management-Suite** (only for the optional last phase)
- `functions/src/services/routeGeometry.service.js` — Mapbox Directions: route line + km/time on the DRS map.
- `functions/src/services/roadDistance.service.js` + `triggers/roadDistance.trigger.js` — Mapbox Directions: planned pin → delivery GPS road distance (500 m check).
- `functions/src/services/roadDistanceMatrix.service.js` (used by `scatterDetection.service.js`) — Mapbox Matrix: far-from-route check.

**Ground truth we already have for testing:** Google road distances for DHRD10230 (77 points, 4,290 pairs, from the 20 Nov test), DHRD10239 (20 points, 380 pairs) and DHRD10240 (from the prod cache). These get saved into `offline_eval/` in Phase 2.

---

## Phase 0 — Decisions (½ day, Akshay + Archana + Mahesh)
1. Hosting: **Compute Engine VM** (recommended: simple, always warm, fixed cost) vs a second Cloud Run service.
2. Map area: **Geofabrik "India – southern zone"** (Bengaluru + neighbours) now; all-India later if needed.
3. Network: VM with **no public IP**; Route-Optimization reaches it over the VPC (Cloud Run Direct VPC egress).
4. Owner for the server, on-call, and the monthly map refresh.
5. Budget: ~$40–80/month per environment (dev can be smaller / stopped when unused).

**Done when:** decisions written into this doc.

## Phase 1 — OSRM server on dev (Archana, 1–2 days)
Steps:
1. VM in the dev project (`hermes-dev-508805`), asia-south1, ~8 GB RAM to start (e.g. e2-standard-2 or e2-highmem-2), Ubuntu + Docker, no external IP, Cloud NAT for downloads.
2. Download `southern-zone-latest.osm.pbf` from Geofabrik.
3. Process with the official `osrm-backend` image, car profile, MLD: `osrm-extract -p /opt/car.lua` → `osrm-partition` → `osrm-customize`.
4. Run `osrm-routed --algorithm mld --max-table-size 150` on port 5000 as a Docker service with restart-always (starts on VM boot).
5. Firewall: allow tcp:5000 only from the Cloud Run egress subnet (and an admin/IAP range for testing).

Tests (all must pass):
- `/route/v1/driving/77.6382,12.8864;77.6572,12.8949` returns `"code":"Ok"` with a sensible distance (Bengaluru hub → a known stop).
- `/table/v1/driving/...` with 101 points returns a 101×101 table in < 1–2 s.
- Reboot the VM → OSRM answers again without manual steps.
- From outside the VPC the port is not reachable.
- Note RAM used after load (sizes the prod VM).

## Phase 2 — Offline accuracy check (Akshay, 1 day, read-only, nothing in prod changes)
1. Save the Google ground truth (DHRD10230 / 10239 / 10240 points + distances) into `offline_eval/data/`.
2. New script `offline_eval/osrm_compare.py`: asks OSRM's `/table` for the same points (**coordinates are lon,lat for OSRM**), with `annotations=distance,duration`, and records each point's snap distance (`waypoints[].distance`).
3. Report:
   - per pair OSRM ÷ Google: median, 90th percentile, share > 25 % off, worst 20 pairs with coordinates;
   - points that snap > 100–200 m from their pin (gated layouts, missing lanes);
   - route level: solve each DRS with the OSRM table, score the order with the Google table; compare with the Google-planned order (DHRD10230 best 82.4 km, DHRD10239 57.2 km).
4. Open the worst pairs/points on a map and note whether OSM is missing a road.

**Go to Phase 3 when:** median per-pair difference ≤ ~10 %, and OSRM-planned routes within ~3 % of Google-planned road km. If not, look at the outliers first (map fixes or keep Google for those areas).

## Phase 3 — Code in Route-Optimization (Akshay, ~2 days, branch `feature/osrm-provider`)
1. `app/config.py`: `OSRM_BASE_URL` (env, empty = OSRM off), `OSRM_TIMEOUT_S` (10), `OSRM_MAX_TABLE_POINTS` (150), `OSRM_MAX_SNAP_M` (200).
2. `app/db/feature_flags.py`: read a value, not just on/off — `featureFlags/roadDistanceProvider` `{ value: "google" | "shadow" | "osrm" }`, default `google`; still only used when `roadDistanceRouting` is on.
3. `app/services/road_matrix.py`:
   - `fetch_osrm_table(points)` → metres + seconds + snap distances; one request up to 150 points, otherwise split with OSRM `sources`/`destinations`.
   - Provider `osrm`: every pair from OSRM. Pairs OSRM can't answer (null, snap > `OSRM_MAX_SNAP_M`) and any OSRM error/timeout → today's Google path (cache, cap, retry) → estimate. So routing never fails.
   - Provider `shadow`: route exactly as today with Google; also ask OSRM and compute comparison stats. Never changes the route; OSRM failures ignored.
4. `routing_runs`: add `provider`, `osrm_pairs`, `osrm_errors`, `osrm_ms`, snap outliers, and in shadow: median/p90 OSRM÷Google and the chosen route's km under both.
5. Tests (fake OSRM): lon/lat order; null cells and big snaps fall back per pair; HTTP error / timeout → Google; flag values and defaults; shadow leaves the order identical; >150 points split correctly; `routing_runs` fields.
6. Deploy config (with Archana — CI/deploy files are infra): add `OSRM_BASE_URL` and Direct VPC egress (`--network`, `--subnet`, `--vpc-egress private-ranges-only`) to the dev workflow first.
7. README + DEPLOYMENT.md: provider flag, OSRM setup, rollback.

## Phase 4 — Dev end-to-end (1–2 days)
1. Deploy Route-Optimization to dev with flag `shadow`.
2. Create dev DRSs from copies of real stops (small ~10, medium ~35, large ~76); optimize and re-optimize.
3. Check `routing_runs`: shadow stats present, `osrm_errors` 0.
4. Flip flag to `osrm`: routes produced, Google calls ≈ 0 (`from_api` 0).
5. **Failure test:** stop the OSRM container → optimize still works via Google; start it again → back to OSRM.
6. Timing: a 76-stop optimize takes no longer than with Google.

## Phase 5 — Prod in shadow (1–2 weeks)
1. Prod OSRM VM (same build; size from Phase 1 RAM numbers) in `prj-dev-hermes`.
2. Deploy Route-Optimization to prod with flag `shadow` — drivers still get Google-based routes.
3. Daily: review `routing_runs` shadow stats; inspect the worst pairs/snaps on the map.

**Go-live when:** same thresholds as Phase 2 hold on real DRSs for 1–2 weeks, and no OSRM errors.

## Phase 6 — Switch prod to OSRM
1. Set `featureFlags/roadDistanceProvider` to `osrm` (no deploy).
2. Watch for a week: Google Routes usage in GCP Metrics drops to ~0; `routing_runs.provider = osrm`; any driver/executive complaints about order.
3. **Rollback:** set the flag back to `google` — instant.

## Phase 7 — Operations (Archana)
- Monthly map refresh (Cloud Scheduler / cron): download → process into a new folder → swap → restart; keep the previous data for rollback.
- Uptime check + alert on the OSRM port; GCP budget alert.
- Runbook in Route-Optimization `DEPLOYMENT.md`.

## Phase 8 — Optional: move 3PL's Mapbox road calls to OSRM
- Route line + km/time (`routeGeometry.service.js`) → OSRM `/route` (returns the line).
- Delivery road distance (`roadDistance.service.js`) → OSRM `/route`.
- Far-from-route check (`roadDistanceMatrix.service.js`) → reuse Route-Optimization's distances or OSRM `/table`.
- Needs functions → VPC access (or go through a Route-Optimization endpoint). Show "© OpenStreetMap contributors" where OSM-based lines are drawn. 3PL change-history + version bumps apply.

## Rough timeline
Phase 0–2: week 1 · Phase 3–4: week 2 · Phase 5: weeks 3–4 · Phase 6: week 5 · Phase 7 alongside · Phase 8 later.

## Risks
- OSM gaps in small lanes / gated layouts → measured in Phases 2 and 5; snap guard + per-pair Google fallback.
- We own uptime and map refresh → Google fallback covers outages.
- No live traffic → same as today.

---

## Update 24 Nov 2026 - decisions and what Phase 3 became

**Decisions (Mahesh, 24 Nov):**
- **No Google fallback for now.** "Let's see based on failure data." OSRM is the only road-distance source once the switch is on `osrm`. If OSRM cannot be reached, that DRS is planned on straight lines (the existing, free safety net) and an alert goes out; the failures are logged in `routing_runs` (`osrm_errors`).
- **Alerting:** Google Chat space; **daily health check at 07:00 IST** (so problems are known before DRS creation) plus an immediate alert whenever an optimize cannot reach OSRM.
- Budget OK: about $65/month per OSRM VM (e2-standard-2, 8 GB, 50 GB disk); dev can be stopped when idle.
- No long shadow period in prod: a few hours of checks, switch is instant to roll back (`featureFlags/roadDistanceProvider`).

**Dev server found (Archana, Phase 1):** VM `osrm-dev`, e2-standard-2, asia-south1-a, internal `10.160.0.2:5000`, network/subnet `default`, no external IP; OSRM v5.27.1, **whole southern-zone** map in `/srv/osrm/2026-11/` (monthly folder); about 3.6 GiB RAM used. Still to confirm: restart policy, reboot test, 101-point table test.

**Phase 3 as built (branch `feature/osrm-provider`, not deployed):**
- `featureFlags/roadDistanceProvider` `{value: google|shadow|osrm}`, default google.
- `app/services/osrm.py`: `/table` in 100x100 blocks, snap guard (200 m), unroutable pairs estimated, `shadow_stats`, `health_check`.
- `app/services/alerts.py`: Google Chat webhook, 30-minute cooldown per alert key, never raises.
- `GET /health/osrm` (503 + alert on failure, `?notify=true` posts a daily OK).
- `routing_runs` gets `provider`, `osrm_ms`, `osrm_requests`, `osrm_errors`, `osrm_unroutable_pairs`, `snap_outliers`, `shadow_*`.
- Changed from the original plan: **the Google road cache is not used in `osrm` mode** (entries would age out in 30 days and mixing sources is hard to reason about), and OSRM answers are not written to it. Shadow sampling and a Google cap are not needed: shadow only adds free OSRM calls.
- Deploy: `deploy-dev.yml` (Direct VPC egress, `OSRM_BASE_URL`, `ALERT_ENV_NAME`, secret `alert-chat-webhook-url`); steps in `DEPLOYMENT.md` section 11. Prod workflow untouched until the prod OSRM VM exists.

**Verified against the real dev-style OSRM (cloud copy, same car profile):** 77-point table in 1.3 s (1 request); 230 points in 9 requests (4.9 s), block answers identical to a single request; shadow stats equal the Phase 2 numbers (median 0.995 / 0.991 / 0.992). Route length vs the Google-planned order, re-run: DHRD10230 +10.2% (earlier run +6.3%), DHRD10239 +7.7%, DHRD10240 +0.9% - the solver is time-limited so runs vary; plan on **1-10%** longer on big DRSs.
