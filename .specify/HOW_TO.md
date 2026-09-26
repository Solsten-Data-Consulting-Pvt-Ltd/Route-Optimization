# How this repo goes from an idea to deployed code

A one-time note for Archana (and anyone else new to this) — neither Pradeep nor I (Claude) knew this tool either before this round, so this is written the way it was actually figured out, not the way a manual would explain it.

## What "Spec Kit" actually is, in plain terms

"Spec Kit" is a name for a way of working, not a magic tool: **write down what you're building and why, before you write the code, in a file everyone can read** — instead of the plan living only in a Slack thread, a person's head, or an AI chat that scrolls away. That file lives at `.specify/spec.md` in this repo. That's genuinely most of it.

The formal version of Spec Kit (the real open-source project this idea comes from) also has slash-commands like `/speckit.specify`, `/speckit.plan`, `/speckit.tasks` that generate a `spec.md`, then a `plan.md`, then a `tasks.md`, from fixed templates. **We don't have that tool installed in this repo** — there's no `.specify/templates/` folder, no `specify` CLI. What we have is a `spec.md` written by hand, in a conversation, shaped around what this specific project actually needed.

**So: is our `spec.md` "the right way" to write one, or is there a standard we should be following instead?**

Honest answer — there is a standard template (a fixed set of headings: Feature overview, User Scenarios, numbered Requirements like FR-001, a Review Checklist, etc.), but we didn't use it, on purpose. Ours has a Glossary, a Blast Radius table, and a Tech Debt section instead, because that's what this repo actually needed — nobody except Pradeep, one Claude conversation, and a prior Gemini chat knew what terms like "HILT" or "master waypoint" even meant, so a glossary mattered more here than a formal FR-001 numbering scheme would have. **Neither is more "correct"** — the standard template is worth adopting later if this team ever wants tooling (like automated cross-checks between spec/plan/tasks) that expects it; until then, a spec.md that people can actually read and act on beats one that follows a template nobody asked for.

## The path from "an idea in spec.md" to "running in production"

Here's the real sequence, using **§5.6 (feature flags) and §5.3 (master-waypoint tier)** as the worked example — because those are the two that actually went through this whole path in this round, today.

### 1. Spec — write down what and why (done: `.specify/spec.md` §5.3, §5.6)

Before any code, `spec.md` says: what problem this solves, what file it'll touch, what it *won't* touch (the "blast radius"), and what's explicitly out of scope. This is the thing to argue about and correct — it's much cheaper to fix a sentence in `spec.md` than a bug in deployed code. Most of the back-and-forth that produced this file was exactly that: catching wrong assumptions here, before any code existed.

### 2. Plan — figure out *how*, file by file (informal here; do it properly for the next feature)

For a bigger or riskier change, this is its own step: list the exact files that change, in what order, and what could go wrong. For §5.3/§5.6 this was simple enough (two new self-contained files, one small edit to an existing function) to go straight from spec to code in one sitting. **§5.1 and §5.2 are not that simple** — they touch the live request path directly (see spec.md §6's risk table) — so those should get a real, written-out plan before anyone starts typing code, not skipped the way this round skipped it.

### 3. Tasks — break the plan into steps small enough to review one at a time

Same idea as above: for a small change, "write the file, write the test, wire it in" is the whole task list. For a bigger one, write it down so a reviewer (or you, a week later) can see the shape of the change before reading a 300-line diff.

### 4. Implement — actually write the code

What got built this round, concretely:

| File | What it does |
|---|---|
| `app/db/feature_flags.py` | Reads on/off switches and settings from a new Firestore collection (`app_config`), cached for ~45 seconds so it's fast but still changes without a redeploy. |
| `app/db/master_waypoint.py` | The actual master-waypoint lookup: given an address, check a small curated list of known-good buildings first, before anything else. |
| `app/services/save.py` (`_geocode()`) | One new block at the top: *if* the flag is on, try the master-waypoint lookup first. *If the flag is off, this repo behaves exactly as it did yesterday* — nothing changes until someone deliberately turns it on. |

That last point is the whole safety idea behind §5.6: **new code can be merged and deployed while doing absolutely nothing**, because it's wrapped in `if feature_flags.is_enabled("master_waypoint_v2"):`. Deploying is not the same thing as turning a feature on.

### 5. Test — prove it works before anyone else has to trust it

`tests/test_feature_flags.py` and `tests/test_master_waypoint.py` — run them with:

```
python3 -m unittest discover -s tests
```

These tests don't touch real Firestore (no credentials needed) — they use a small fake in-memory version of it, which is why they run in a fraction of a second and can run anywhere, including here.

### 6. Review & deploy — same as this repo already does

Normal PR review, then deploy the same way this repo always deploys (see `DEPLOYMENT.md`). **Deploying does not turn anything on** — see step 4. The code goes live disabled.

### 7. Turn it on — this is the step that's actually new to this repo

This is the part that didn't exist before §5.6. To actually enable master-waypoint lookups in production:

1. In the Firestore project this repo uses, open the `app_config` collection, document `master_waypoint_v2`.
2. Set `enabled` to `true`.
3. Wait up to ~45 seconds (the cache TTL) — no redeploy needed.
4. Watch it: `/save-consignments`'s response now includes a `masterWaypointHits` count per DRS in `cache_metrics` — if it's a real feature working, you'll see that number go up.
5. **If anything looks wrong, set `enabled` back to `false`.** Same ~45-second delay, no redeploy, no rollback commit. That's the entire point of building it this way — reverting a bad decision should be as fast and boring as flipping a switch back.

There's no admin screen for this yet — today it's a manual edit in the Firestore console (or a one-off script calling `app.db.feature_flags.seed_default_flags()` to create the doc first, if it doesn't exist). That's a reasonable next thing to build once more than one or two flags are in real use.

## What's built vs. what's still just written down

- **Built and tested today:** §5.3 (master-waypoint tier), §5.6 (feature flags). Off by default in prod.
- **Written in `spec.md` but not built yet:** §5.1 (geocoding ambiguity → HILT), §5.2 (solitary-outlier trap), §5.4 (EOD feedback endpoint), §5.5 (BQML embeddings evaluation). Each has its own section in `spec.md` with what it'll touch and the risk involved — read that section before starting it, the same way this round did for §5.3/§5.6.
- Before starting any of those: check `spec.md` §8 (Tech Debt) — a couple of items there (unparameterized SQL in `bigquery.py`, no test coverage on `sorting.py`) are flagged as things to fix *as part of* §5.2 specifically, not separately.

## The one rule worth remembering

**`spec.md` is the single source of truth.** If something's decided, unclear, or changed, it goes there — not in a chat that scrolls away, not in a Slack message, not only in a code comment. If you're ever unsure what a term like "HILT" means, or why a threshold is 10km and not 5km, the answer should already be in `spec.md`'s Glossary or the relevant §5.x section. If it isn't, that's a gap in the doc worth fixing before writing more code against it.
