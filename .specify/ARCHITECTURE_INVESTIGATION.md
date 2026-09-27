# Architecture Investigation — Handover Prompt

**Purpose of this document:** this is a **feeder prompt for an investigation phase**, not an implementation order. Whoever (or whichever Claude session) picks this up should research, propose options with tradeoffs, and get sign-off — the same discipline `.specify/spec.md` and `.specify/HOW_TO.md` were built with in this repo — before writing code. Do not start refactoring on read of this file.

**How to use this file:** paste it as the opening prompt to a new investigation session, or hand it to a teammate. It contains: what's already true about this repo (so you don't have to re-derive it), six investigation objectives, and what "done" looks like for the investigation phase (a findings/options document, not a diff).

---

## 0. Read first

- `.specify/spec.md` — the current functional spec (address resolution, HILT, EOD feedback, config/feature flags). Two sections (§5.3, §5.6) are implemented; the rest are planned.
- `.specify/HOW_TO.md` — plain-language walkthrough of how this repo goes from spec to deployed, flag-gated code.
- PR #3 (`claude/cloud-browser-task-pgrfzz` → `main`) — currently has a real, unresolved merge conflict: two independent, overlapping designs (`master_waypoint.py` vs. `drs_memo.py`/`address_match.py`) were built in parallel because there was no shared visibility into in-flight work. **This is itself a case study for objective E below** — treat it as evidence, not just a blocker to route around.

## 1. What's already true about this repo (grounded, not assumed)

- **Stack:** FastAPI on Cloud Run, Firestore (operational + cache collections) + BigQuery (`consignments_routing`, `consignments_structured`), Google Places API v1, OR-Tools. Single service, currently ~2-3 real endpoints (`/save-consignments`, `/run-sorting`, plus whatever `drs_memo`/`address_match` added on `main`).
- **Auth exists at the infra layer, not the app layer.** Both `deploy-dev.yml` and `deploy-prod.yml` deploy with `--no-allow-unauthenticated`, so Cloud Run's IAM gate requires a valid Google-signed identity token just to reach the service at all — correction to an earlier draft of this document, which wrongly said no auth existed anywhere. What's still true: **inside** the app, once a caller is through that gate, there is zero authorization model — no identity, role, or tenant is extracted from the request anywhere in `app/`, and CORS is wide open (`allow_origins=["*"]`, `app/main.py`). Any IAM-permitted caller can call either endpoint for any DRS/consignment with no further scoping. This is the concrete gap to start from for §2 and especially §4 (a tenant boundary needs *something* to check against a request, and today there's nothing).
- **No tenancy concept anywhere.** Grepped the full codebase for `tenant`/`client_id`/`clientId` — zero hits. Every Firestore collection (`consignments`, `consignments_routing`, `drs_starting_point`, `geocode_cache`, `master_waypoints`, `app_config`, and `main`'s new `drs_address_memo`) and the BigQuery tables are single global namespaces. If multi-tenancy is a real near-term requirement, this is a from-scratch design, not a bolt-on (see §4).
- **No observability tooling.** `app/main.py` sets up plain `logging.basicConfig` to stdout (captured by Cloud Run). No OpenTelemetry, no Prometheus, no trace/correlation IDs anywhere. The only "metrics" that exist are two Firestore analytics collections (`drs_cache_metrics`, written by `app/db/cache_metrics.py`) — ad hoc, not a real observability stack.
- **Testing:** `.github/workflows/ci.yml` genuinely runs `python -m unittest discover -s tests -v` on every PR/push to `dev`/`main`. Real, but narrow — today's tests are pure-unit (`geocache` normalization/fuzzy matching, `address` helpers, and this session's `feature_flags`/`master_waypoint` tests using in-memory Firestore fakes). Nothing exercises `sorting.py`, `tsp.py`, the BigQuery write functions, or the FastAPI routes themselves (no `TestClient`-based route tests found outside `main`'s newer `test_geocode_preview_and_bypass.py`, which you should check).
- **SDLC:** `.specify/spec.md`/`HOW_TO.md` are hand-rolled this session, not the canonical GitHub Spec Kit template (no `.specify/templates/`, no `specify` CLI installed). That was a deliberate choice for this repo's needs (glossary, blast radius, tech debt) over the standard FR-numbered template — worth revisiting now that the scope is growing.
- **A real production incident already happened and was fixed without any of the above infrastructure**: commit `af5c6e7` on `main` (case `CCU501619425`) — the sort pipeline was silently reverting manually-corrected delivery pins. It was caught, root-caused, fixed, and given a regression test — entirely through manual engineering effort, with no SRE harness, no alerting, no structured incident record. Good anchor for objective F.

## 2. Objective — OWASP / security hardening

Start from what's real, not a generic checklist pass:
- **A01 Broken Access Control / A07 Identification & Authentication Failures**: no auth exists today. Investigate what identity model fits (service-to-service API keys? OIDC from a calling system? Per-tenant credentials, once §4 is decided?) — this should probably be decided *together* with the multi-tenancy work, not before it, since "who is the caller" and "which tenant are they" are related questions.
- **A03 Injection**: `.specify/spec.md` §8 (Tech Debt, TD-4) already documents a live example — `app/db/bigquery.py`'s `write_group_assignments` builds SQL via raw f-string interpolation instead of parameterized queries, unlike every other query in the same file. A real, fixable finding, not hypothetical.
- **A04/A05 Insecure Design / Security Misconfiguration**: CORS wide open, thin/unvalidated Pydantic request models (`SaveConsignmentsRequest`/`RunSortingRequest` barely constrain input shape).
- Also worth an API-specific pass (OWASP API Security Top 10, not just the general Top 10) given this is a pure API service with no UI.
- **Deliverable expected:** a prioritized findings list (like TD-4, TD-1 etc. in spec.md §8) with severity, not a rewrite.

## 3. Objective — Observability

- Structured logging (today's log lines are free-text `logger.info("...")` calls — no consistent fields, no correlation ID threading a request through `save.py` → `geocache.py`/`master_waypoint.py` → BigQuery/Firestore writes).
- Distributed tracing (OpenTelemetry is the natural fit for Cloud Run + BigQuery + Firestore + an external HTTP call to Places — a trace would show exactly where time goes in one `/save-consignments` call).
- Metrics: today's `drs_cache_metrics` Firestore collection is a bespoke, write-only analytics log, not a real metrics pipeline (no dashboards, no alerting hooked to it as far as this investigation can see). Decide whether to keep it as a business-analytics record *and* add real infra metrics (Cloud Monitoring / Prometheus-compatible), or fold one into the other.
- This objective directly feeds objective F (SRE harness needs signals to alert on).

## 4. Objective — Multi-tenancy: define the 3 levels

This is the objective needing the most careful, validated definition — **don't treat the split below as settled, treat it as the starting hypothesis to pressure-test**, using this repo as the worked example:

| Level | What it is | Candidate contents from this repo |
|---|---|---|
| **1 — Base SaaS platform service** | Shared substrate every app and every tenant rides on. Infra and cross-cutting concerns, no business logic. | `app/db/clients.py` (Firestore/BQ client wrappers), the new `app/db/feature_flags.py` (§5.6), auth/authz middleware (once §2 lands), observability middleware (once §3 lands), the config pattern itself (`app/config.py`) |
| **2 — App** | One product/service running on the platform. | Route-Optimization itself — geocoding, master-waypoint, TSP, sorting — is one app. A second product later (billing, a different logistics workflow) would be a second app reusing Level 1. |
| **3 — App-tenancy** | Per-client isolation *within* an app. | **Currently missing entirely.** `consignments`, `consignments_routing`, `drs_starting_point`, `geocode_cache`, `master_waypoints`, `app_config` are all single global namespaces with zero tenant field today. |

Open questions the investigation must resolve, not assume:
1. Is Level 3 isolation **row-level** (a `tenantId` field + query scoping, cheaper, more code discipline required to never leak a query across tenants) or **infra-level** (a BQ dataset / Firestore database per tenant, stronger isolation, real cost and operational overhead)? Given BigQuery/Firestore's actual multi-tenancy cost/complexity tradeoffs, this deserves a real comparison, not a default.
2. Is "tenant" = a separate logistics client company, or a depot/business-unit within one client, or both (nested)? This changes the data model significantly.
3. Is this a near-term real requirement (a second client is signed and waiting) or an architectural bet on future growth? That materially changes how much to build now vs. design-for-later.

## 5. Objective — Testing methodology

Build on what's proven, don't discard it: `tests/test_feature_flags.py`/`test_master_waypoint.py` (this session) show the pattern — pure-Python fakes for Firestore, no live credentials needed, fast (all 42 existing tests run in ~0.01s). Extend it:
- Route-level tests using FastAPI's `TestClient` (main's `test_geocode_preview_and_bypass.py` may already have started this — check it).
- Integration tests against Firestore/BigQuery emulators for the parts that are genuinely hard to fake well (the BigQuery MERGE/UPDATE SQL, especially given TD-4).
- Coverage: CI runs tests today but doesn't gate on coverage or fail loudly on an uncovered new file — `sorting.py`, `tsp.py`, and the BigQuery write functions currently have zero tests (spec.md §8, TD-6).
- Load/perf testing for the OR-Tools solve, which is explicitly time-boxed by stop count (`_time_limit_for` in `tsp.py`) — worth knowing where that boundary actually breaks down at scale.

## 6. Objective — SDLC methodology (Spec Kit-like) + Aha!/Jira as inputs

- Decide whether to formalize on the canonical GitHub Spec Kit template/tooling now that scope is growing beyond one repo's worth of features, or keep the hand-rolled `spec.md`/`HOW_TO.md` approach deliberately (both are legitimate — see the "is spec.md written the right way" discussion already had in this session).
- **The Aha!/Jira bridge needs your team's actual usage patterns as input — I have zero visibility into your Aha! or Jira instances.** Don't assume the shape of this; investigate concretely: does an Aha! Feature become a `spec.md` section by hand, or via their API? Does a Jira epic map to a `tasks.md` checklist? Is a sync tool worth building, or is a documented human-curated bridge (like this repo's current process) good enough at current team size?
- PR #3's conflict (master_waypoint vs. drs_memo) is the concrete failure case to design against: **the SDLC methodology's job is to make that collision visible before two people build overlapping things independently**, not just to produce nicer documents after the fact.

## 7. Objective — SRE + support-ticket-handler harness

Anchor in the real incident, not a hypothetical: commit `af5c6e7` (case `CCU501619425`) was caught, root-caused, and fixed entirely by manual engineering effort. Investigate what a harness would need to shorten that loop next time:
- An incident/ticket intake schema — spec.md's §2 (post-delivery audit) and HILT concepts (§5.1) are candidate data sources: a `>500m` delta or a HILT quarantine could be a *leading* indicator, not just a post-hoc report.
- Alerting thresholds tied to objective 3's observability layer.
- A guaranteed link back to regression tests (objective 5) — `af5c6e7` added one manually; the harness's job is to make that the automatic default, not something an engineer has to remember.
- Decide whether this is a human-run runbook first, or something closer to an automated triage agent (a Claude Code session subscribed to a ticket queue, analogous to how this session watches PR #3) — don't over-build the automation before the manual process is proven.

## 8. What "done" looks like for this investigation phase

**Not code.** A findings/options document per objective (or one combined doc), in the same style as `.specify/spec.md` was built: grounded in real files/line numbers where possible, options with explicit tradeoffs (not a single prescribed answer) for the genuinely open questions (especially §4's tenancy-isolation choice and §6's Aha!/Jira bridge), and a proposed blast radius before any of it starts touching production code. Get sign-off on that document before implementation planning begins — the same `/speckit.plan`-after-`/speckit.specify` discipline already established here.

## 9. Explicitly not yet known — ask before assuming

- Current Aha!/Jira usage patterns and API access.
- Whether multi-tenancy (§4) is a committed near-term requirement or a forward-looking design bet.
- Budget/timeline constraints on this investigation and any resulting build.
- Whether an SRE/support function already exists organizationally, or this is greenfield.
- Who owns final sign-off on the tenancy-isolation and SDLC-tooling decisions (likely not a call for whichever session runs this investigation to make alone).
