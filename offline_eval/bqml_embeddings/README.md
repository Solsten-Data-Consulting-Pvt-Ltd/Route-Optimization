# §5.5 — BQML address-embedding evaluation (offline)

Answers one question before anyone wires embeddings into `/save-consignments`:
**when the master-waypoint tier (§5.3) and `geocache.py` both miss, can a
text-embedding match — searched only inside the address's pincode (or
geohash-6 cell) — find the same place without sending parcels to the wrong
building?** The output is a precision/recall report. Nothing here runs in the
live request path, and the container never contains this folder (the
Dockerfile copies only `app/`).

## What it does

| Step | File | Result |
|---|---|---|
| `setup` | `sql/00_setup.sql` | dataset `route_opt_eval` + BQML remote model over `text-embedding-005` |
| `corpus` | `sql/01_labels_*.sql`, `sql/02_corpus.sql` | `corpus`: every consignment whose true pin is known, with its normalised address text, address pincode, geohash-6 and door numbers |
| `neighbours` | `sql/02b_pincode_neighbours.sql` | `pincode_neighbours`: neighbouring-pincode pairs — the reviewed ones from `app/data/pincode_neighbours.json` plus pairs learned from your data (listed in the report for review) |
| `prior` | `run_eval.py` (`prior_tier_hits`) | `prior_tier_check`: would master-waypoint or the verified geocache have answered it at scan time? Uses the live code's own `normalize_address` and the 85 `token_sort_ratio` cutoff |
| `embed` | `sql/03_embed.sql` | `embeddings`: `ML.GENERATE_EMBEDDING`, `task_type = 'SEMANTIC_SIMILARITY'`; incremental, only new/changed text is sent |
| `evaluate` | `sql/04_evaluate.sql` | replays the last `--query-days` days: each consignment against addresses labelled **before** it, **in its own partition**; nearest by cosine distance; scored per threshold into `results` |
| `report` | `run_eval.py` | `reports/<run>.md`, `<run>.csv`, `<run>_mistakes.csv` (gitignored) |

**Ground truth (labels).**

1. `delivery_audit` (§5.4), when it exists: `reasonCode` `A`/`D` → the corrected pin (else the driver's GPS); no reason code and delivered within `--accurate-within-m` (100 m) of the plan → the planned pin was right. `B`, `C` and `NOT_PROVIDED` beyond that give no label.
2. Until then, and alongside it: pins an executive/admin placed by hand (`consignments_routing.locationOverridden = TRUE`).

Two consignments are "the same place" when their true pins are within `--same-place-m` (50 m).

**The `delivery_audit` columns this expects** (spec §5.4, named like `EodDeliveryFeedbackRequest`): `consignmentId`, `drsNo`, `plannedLatitude`, `plannedLongitude`, `actualLatitude`, `actualLongitude`, `deltaMetres`, `reasonCode`, `eventTimestamp`, and optionally `correctedLatitude`, `correctedLongitude`. The runner checks these before running and names any that are missing.

## Guarantees (tested in `tests/test_bqml_embeddings_eval.py`)

- **Never unconstrained.** The only `ML.DISTANCE` is in `04_evaluate.sql`, inside a join on `{{scope_predicate}}`; there is no "no scope" option. Addresses without a pincode in the text are counted and never searched.
- **No leakage.** Candidates must have been labelled before the replayed consignment was created; the pincode scope uses the pincode written in the address (known before geocoding).
- **Flag-gated.** Without `app_config/bqml_embeddings_eval.enabled = true` the runner exits without touching BigQuery or Vertex AI. `--dry-run` prints the SQL and needs no flag or credentials.
- **No production path** under `app/` references any of this.

## Scopes

- `pincode` (default) — the realistic live setting: the pincode is in the scanned text before any geocoding.
- `pincode_neighbours` — the written pincode **plus its neighbouring pincodes** (from the `neighbours` step). For labels like Shahi Exports, Ambalipura: written 560102, really 560103. Still a bounded set of partitions, never the whole corpus. Run it next to `pincode` to see how many repeats the written pincode alone misses.
- `geohash6` — partition = geohash-6 of the consignment's **first** resolved pin (`planned_latitude/longitude`). This measures "re-check a Places pin against known places nearby" (a §5.1 assist), not API savings, since a pin already exists. Requires those columns.

## Running it (dev first)

1. **Infra (once, by whoever manages GCP)** — in the same location as the routing dataset:
   ```
   bq mk --connection --location=asia-south1 --project_id=prj-dev-hermes \
      --connection_type=CLOUD_RESOURCE vertex_embeddings
   bq show --connection prj-dev-hermes.asia-south1.vertex_embeddings   # copy serviceAccountId
   gcloud projects add-iam-policy-binding prj-dev-hermes \
      --member="serviceAccount:<serviceAccountId>" --role="roles/aiplatform.user"
   ```
   Check that `text-embedding-005` is offered in that region.
2. **Turn the flag on** in dev Firestore: `app_config/bqml_embeddings_eval` → `enabled: true` (`seed_default_flags()` creates it disabled).
3. **Run**, with the app's usual env vars (`BQ_PROJECT`, `BQ_DATASET`, `BQ_TABLE`, `FIRESTORE_PROJECT`, …) and gcloud application-default credentials:
   ```
   python -m offline_eval.bqml_embeddings.run_eval --location asia-south1 \
       --connection prj-dev-hermes.asia-south1.vertex_embeddings \
       --steps setup,corpus,prior,embed,evaluate,report
   ```
   Later runs skip `setup`. See exactly what will run first with `--dry-run`.
4. **Read the report**, then `reports/<run>_mistakes.csv` — every wrong-building call at the recommended threshold. Those decide go/no-go, not the averages.

Cost: embeddings are billed per character sent (each address once; re-runs only send new text); the evaluation join is bounded by partition sizes. Turn the flag off afterwards.

## Neighbouring pincodes

A pair (A, B) is **learned** when at least `--min-pair-count` (5) saved pins whose label says A sit in Google's pincode B, and those pins are typically (median) within `--neighbour-max-m` (6 km) of where A really is: the centroid of pins whose label and Google pincode agree. The distance test separates "the pincode next door" from wrong pins across the city. Learned pairs appear at the end of the report. Once a person has checked them, add them to `app/data/pincode_neighbours.json`, which is the same file the live code uses (`app/services/pincode_neighbours.py`).

## Reading the result

The recommendation is the highest-recall threshold, on the `prior_tiers_missed` slice with the door-number guard, whose precision is at least `--min-precision` (99.5%) over at least 20 calls. "No threshold qualifies" is a valid, useful answer: do not wire it in. Wiring it into `save.py` is a separate, flag-gated change (§5.5 → production), not part of this folder.
