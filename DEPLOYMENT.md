# Deployment Guide

How this service gets from your laptop to Cloud Run, what every GitHub secret
and variable is for, and what to do when you need to ship a fix.

If you only want the short version, jump to
[Day-to-day: how to ship a change](#day-to-day-how-to-ship-a-change).

---

## 1. The big picture

There are **two environments**, and they are completely separate — different GCP
projects, different Cloud Run services, different data.

| | Development | Production |
|---|---|---|
| Git branch | `dev` | `main` |
| GitHub environment | `development` | `production` |
| Cloud Run service | `route-optimization-dev` | `route-optimization` |
| Region | `asia-south1` | `asia-south1` |
| GCP project | from variable `GCP_DEV_PROJECT_ID` (e.g. `hermes-dev-508805`) | from variable `GCP_PROD_PROJECT_ID` |
| Workflow file | `.github/workflows/deploy-dev.yml` | `.github/workflows/deploy-prod.yml` |

**The rule is simple: the branch decides the environment.**

```
push to dev   ->  deploys to development
push to main  ->  deploys to production
open a PR     ->  runs tests only, deploys nothing
```

Nobody runs a deploy command by hand. Merging is the deploy.

---

## 2. What happens when you push

There are three workflow files in `.github/workflows/`. Here is what each one
does and when it wakes up.

### `ci.yml` — Continuous integration (the safety net)

**Runs on:** any pull request into `dev` or `main`, and any push to `dev` or `main`.

**What it does:**

1. Checks out the code.
2. Installs Python 3.11 and everything in `requirements.txt`.
3. Runs `python -m unittest discover -s tests -v`.

**It never deploys anything.** Its whole job is to tell you *before* merging
whether the tests still pass. If CI is red on your PR, fix it before merging.

### `deploy-dev.yml` — Deploy development

**Runs on:** a push to `dev` (which in practice means "a PR was merged into `dev`").

**What it does:**

1. Checks out the code.
2. Logs in to the dev GCP project using OIDC (explained in section 3).
3. Runs one `gcloud run deploy` command that builds and rolls out
   `route-optimization-dev`.

It has `cancel-in-progress: true`. If you merge twice quickly, the older deploy
is cancelled and only the newest one lands. That is fine for dev.

### `deploy-prod.yml` — Deploy production

**Runs on:** a push to `main`.

Same three steps, but against the production project and the
`route-optimization` service.

It has `cancel-in-progress: false`. Production deploys **queue up** instead of
cancelling each other, so a half-finished rollout is never killed mid-way.

---

## 3. How GitHub is allowed to touch GCP (OIDC — no JSON keys)

This is the part people find confusing, so here it is in plain words.

**The old way** was to download a service-account JSON key file from GCP and
paste it into a GitHub secret. That works, but the key never expires. If it ever
leaks, whoever has it can deploy to your project forever.

**The way this repo does it** is called **Workload Identity Federation**, using
OIDC. There is no key file anywhere.

What actually happens during a deploy:

1. GitHub Actions generates a short-lived, signed **identity token** that says:
   *"this job is running in the `Solsten-Data-Consulting-Pvt-Ltd/Route-Optimization`
   repository, on branch `main`."*
2. The workflow hands that token to GCP's **Workload Identity Provider**.
3. GCP checks the token's signature and its claims against rules you configured
   when you set the provider up ("only trust tokens from this repo").
4. If it matches, GCP says *"OK, you may act as this service account"* and hands
   back an access token that is valid for a few minutes.
5. `gcloud` uses that short-lived token to deploy, and it expires on its own.

That is why `deploy-dev.yml` and `deploy-prod.yml` both declare:

```yaml
permissions:
  contents: read
  id-token: write
```

`id-token: write` is what allows GitHub to mint that identity token. Without it
the deploy fails at the auth step. `contents: read` lets it check out the code.

**Two service accounts are involved. Don't mix them up:**

| | Deploy service account | Runtime service account |
|---|---|---|
| Who uses it | GitHub Actions, during the deploy | The running Cloud Run container |
| What it needs to do | build the image, create a revision, read the Maps secret | query BigQuery, read/write Firestore |
| Named in | the `GCP_*_SERVICE_ACCOUNT` secret | the Cloud Run service config |

---

## 4. GitHub secrets and variables — what each one is

Set these in the repository under **Settings → Secrets and variables → Actions**.

There are two tabs there, and the difference matters:

- **Secrets** — hidden after you save them, masked in logs. Use for anything
  sensitive.
- **Variables** — plain text, visible, printable in logs. Use for non-sensitive
  config like a project ID.

### Secrets (4)

| Secret | What it is | Where you get it | If it's missing / wrong |
|---|---|---|---|
| `GCP_DEV_WORKLOAD_IDENTITY_PROVIDER` | Full resource name of the dev Workload Identity Provider. Looks like `projects/123456789/locations/global/workloadIdentityPools/github/providers/github-provider` | GCP Console → IAM & Admin → Workload Identity Federation, in the dev project | Dev deploy fails at the `google-github-actions/auth` step |
| `GCP_DEV_SERVICE_ACCOUNT` | Email of the service account GitHub impersonates for dev, e.g. `github-deployer@hermes-dev-508805.iam.gserviceaccount.com` | GCP Console → IAM & Admin → Service Accounts, dev project | Same — auth step fails, or you get a permission error |
| `GCP_PROD_WORKLOAD_IDENTITY_PROVIDER` | Same thing, for the production project | Production project's Workload Identity Federation page | Production deploy fails at auth |
| `GCP_PROD_SERVICE_ACCOUNT` | Deploy service-account email for production | Production project's Service Accounts page | Same |

> **Note:** the project number in the provider resource name is the **numeric**
> project number, not the project ID string. Copying the ID instead of the
> number is the single most common setup mistake.

### Variables (2)

| Variable | What it is | Current value | If it's missing |
|---|---|---|---|
| `GCP_DEV_PROJECT_ID` | Dev GCP project ID. Used as `--project`, and also passed into the app as `BQ_PROJECT` and `FIRESTORE_PROJECT` | `hermes-dev-508805` | `gcloud` gets an empty `--project` and the deploy fails immediately |
| `GCP_PROD_PROJECT_ID` | Production GCP project ID, used the same way | `prj-dev-hermes` | Production deploy fails immediately |

**These two are the only variables the workflows read.** The region
(`asia-south1`), the Cloud Run service names, and the Maps secret name
(`google-maps-api-key`) are written directly into the workflow YAML, not read
from variables. So if you change one of those in the GitHub Variables tab,
nothing happens — edit the workflow file instead.

**Why the project ID is a variable and not a secret:** it is not sensitive, and
keeping it visible means you can actually read the deploy logs and see which
project a revision went to. Masking it would just make debugging harder.

**Why it's one value used three times:** the workflow feeds `GCP_PROJECT_ID` into
`--project`, `BQ_PROJECT` *and* `FIRESTORE_PROJECT`. So the design assumes
BigQuery and Firestore live in the **same project** as the Cloud Run service. If
that ever stops being true, the workflow has to change — you cannot fix it from
the Cloud Run console alone, because the next deploy would overwrite you.

### Quick checklist

```
Settings -> Secrets and variables -> Actions

  Secrets tab:
    [ ] GCP_DEV_WORKLOAD_IDENTITY_PROVIDER
    [ ] GCP_DEV_SERVICE_ACCOUNT
    [ ] GCP_PROD_WORKLOAD_IDENTITY_PROVIDER
    [ ] GCP_PROD_SERVICE_ACCOUNT

  Variables tab:
    [ ] GCP_DEV_PROJECT_ID
    [ ] GCP_PROD_PROJECT_ID
```

Any other variable present in the repo is not read by these workflows.

---

## 5. The deploy command, flag by flag

Both deploy workflows run one command. This is the production one:

```bash
gcloud run deploy route-optimization \
  --project "$GCP_PROJECT_ID" \
  --region asia-south1 \
  --source . \
  --no-allow-unauthenticated \
  --timeout 300 \
  --update-env-vars BQ_PROJECT="$GCP_PROJECT_ID",FIRESTORE_PROJECT="$GCP_PROJECT_ID",BQ_DATASET=Hermes_Exports,BQ_TABLE=consignments_routing,BQ_STRUCTURED_TABLE=consignments_structured \
  --set-secrets GOOGLE_MAPS_API_KEY=google-maps-api-key:latest
```

| Flag | What it means |
|---|---|
| `run deploy route-optimization` | The Cloud Run service name. Dev uses `route-optimization-dev` |
| `--project` | Which GCP project. Comes from the repository variable |
| `--region asia-south1` | Mumbai. Keep the service near the data |
| `--source .` | **Build from source.** The repo is uploaded to Cloud Build, which builds an image and deploys it. Because a `Dockerfile` exists at the repo root, that Dockerfile is used (Python 3.11-slim, `pip install -r requirements.txt`, run uvicorn) |
| `--no-allow-unauthenticated` | The service is **private**. Every request needs a Google identity token and the caller needs `roles/run.invoker`. This is why testing needs `gcloud auth print-identity-token` |
| `--timeout 300` | A request can run up to 5 minutes. Geocoding a large batch is slow, so this is deliberately high |
| `--update-env-vars` | Sets these environment variables on the new revision. `update` **merges** — variables already on the service that are not in this list are kept |
| `--set-secrets` | Mounts Secret Manager secret `google-maps-api-key`, version `latest`, as the env var `GOOGLE_MAPS_API_KEY`. The key value never appears in git or in the workflow |

### Where configuration actually lives

This is worth being clear about, because it changed:

| Setting | Controlled by |
|---|---|
| `BQ_PROJECT`, `FIRESTORE_PROJECT`, `BQ_DATASET`, `BQ_TABLE`, `BQ_STRUCTURED_TABLE` | **The workflow file.** Editing them in the Cloud Run console works only until the next deploy, which overwrites them |
| `GOOGLE_MAPS_API_KEY` | **Secret Manager**, secret name `google-maps-api-key`. Rotate it there — no deploy needed if you add a new version, since the service pins `:latest` and picks it up on the next revision |
| `PORT` | Cloud Run sets it automatically; the Dockerfile defaults it to 8080 |
| Anything else (memory, CPU, concurrency, min instances) | The Cloud Run console. The workflow does not set these, so console changes survive |

**So: to change a table name or dataset, edit the workflow YAML and merge it.**
Don't change it in the console.

### `Procfile` vs `Dockerfile`

Both files exist in the repo. Since `--source .` finds a `Dockerfile`, Cloud
Build uses **the Dockerfile** and ignores the `Procfile`. The `Procfile` is a
leftover from the buildpacks setup. Harmless, but don't waste time editing it
expecting a change.

---

## 6. One-time GCP setup (per environment)

You only do this once per project. If deploys already work, skip this section.

1. **Enable APIs** in the project: Cloud Run, Cloud Build, Artifact Registry,
   Secret Manager, IAM Credentials, BigQuery, Firestore.

2. **Create a Workload Identity Pool and Provider**, configured to trust GitHub's
   OIDC issuer and restricted to this repository. Restricting by repository
   matters — an unrestricted provider would let *any* GitHub repo deploy to your
   project.

3. **Create the deploy service account** and grant it, at minimum:

   | Role | Why |
   |---|---|
   | Cloud Run Admin | create and update the service |
   | Service Account User | act as the Cloud Run runtime service account |
   | Cloud Build Editor | run the source build |
   | Artifact Registry Writer | push the built image |
   | Secret Manager Secret Accessor | read `google-maps-api-key` during deploy |

4. **Allow the GitHub repo to impersonate it** — bind the deploy service account
   to the Workload Identity principal for this repository
   (`roles/iam.workloadIdentityUser`).

5. **Create the Maps secret** in Secret Manager, named exactly
   `google-maps-api-key`. Both workflows expect that name.

6. **Grant the runtime service account** (the one the container runs as)
   BigQuery Data Editor on the routing table, read access on
   `consignments_structured`, and Firestore read/write.

7. **Add the GitHub secrets and variables** from section 4.

8. **Disable the old Cloud Build trigger for `main`** if it still exists.
   Otherwise Cloud Build and GitHub Actions will both deploy production from the
   same commit and race each other.

---

## 7. Day-to-day: how to ship a change

This is the normal flow for a bug fix, a small feature, anything.

### Step 1 — Start from the latest `dev`

```powershell
git checkout dev
git pull origin dev
```

Always branch off `dev`, not `main`. `main` is what is live in production; `dev`
is what is coming next.

### Step 2 — Make a branch

```powershell
git checkout -b fix/sorting-skips-delivered
```

Use a readable prefix so the branch list stays scannable:

| Prefix | For |
|---|---|
| `fix/` | a bug fix |
| `feat/` | new behaviour |
| `chore/` | dependency bumps, config, docs |

**Never commit straight to `dev` or `main`.**

### Step 3 — Change the code and test locally

See **Local run** in the main `README.md`. Then run the tests the same way CI
will:

```powershell
python -m unittest discover -s tests -v
```

If your change touches sorting, save, or the BigQuery queries, also hit the
local endpoints with a real DRS before opening a PR. CI only runs unit tests —
it does not talk to BigQuery, so it cannot catch a bad query.

### Step 4 — Commit and push

```powershell
git add .
git commit -m "Keep delivered stops in their existing sequence slot"
git push -u origin HEAD
```

Write the commit message about **why**, not what. The diff already shows what.

### Step 5 — Open a PR into `dev`

On GitHub, open a pull request from your branch into **`dev`**.

CI runs automatically. Wait for the green tick. A red CI means merging will ship
a known-broken build to development.

### Step 6 — Merge into `dev`, which deploys development

Merge the PR. `deploy-dev.yml` fires immediately and deploys
`route-optimization-dev`.

Watch it under the **Actions** tab. A source deploy usually takes a few minutes
because it builds the image from scratch.

### Step 7 — Verify on development

```powershell
$base  = "https://<your-dev-service-url>"
$token = gcloud auth print-identity-token

curl.exe -sS "$base/health" -H "Authorization: Bearer $token"
```

Then exercise the endpoint you actually changed with a real DRS. See the
**Testing** section of the main `README.md` for full curl and Postman examples.

**Do not skip this step.** Development exists precisely so that a bad change
stops here.

### Step 8 — Promote to production

Open a **second** pull request, this time from `dev` into `main`.

```
your branch  ->  dev   (deploys development)
dev          ->  main  (deploys production)
```

Get it reviewed. Merging into `main` fires `deploy-prod.yml` and it is live.

### Step 9 — Verify production

Same health check and the same endpoint test, against
`https://route-optimization-567483485783.asia-south1.run.app`.

### Hotfix (production is broken right now)

The clean path is still through `dev` — it is only a few extra minutes and it
keeps the branches from diverging. Branch off `dev`, fix, PR to `dev`, verify on
development, then PR `dev` into `main`.

If production is truly on fire and you cannot wait, prefer **rolling back**
(section 8) over branching straight off `main`. A rollback is instant and
carries zero risk of a new bug. Fix forward afterwards, calmly.

---

## 8. Rolling back

Cloud Run keeps every revision. Rolling back means pointing traffic at the
previous one — no rebuild, takes seconds.

**Console:** Cloud Run → the service → **Revisions** → find the last known-good
revision → **Manage traffic** → send 100% to it.

**Command line:**

```bash
gcloud run revisions list --service route-optimization --region asia-south1 --project <PROJECT_ID>

gcloud run services update-traffic route-optimization \
  --region asia-south1 --project <PROJECT_ID> \
  --to-revisions <GOOD_REVISION_NAME>=100
```

Two things to remember:

- A rollback changes the **code**, not the data. Anything already written to
  BigQuery or Firestore by the bad revision stays written.
- Your rollback stays in place until the next merge to `main` deploys a new
  revision. So revert or fix the offending commit in git too — otherwise the
  next unrelated merge quietly re-ships the bug.

---

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Deploy fails at `google-github-actions/auth` | Wrong provider resource name, wrong SA email, or the repo isn't bound to the provider | Re-check the two `GCP_*` secrets; confirm the provider uses the numeric **project number** |
| `Permission denied` during deploy | Deploy SA is missing a role | Compare against the role list in section 6 |
| Deploy succeeds, service returns `500` on startup | A required env var is missing — `config.py` reads them all with `os.environ[...]` and has no defaults | Check the revision's variables; the workflow must set all five |
| `401` / `403` calling the service | No token, expired token, or your user lacks `roles/run.invoker` | Re-run `gcloud auth print-identity-token` (they last about an hour) |
| `500` with BigQuery `Name ... not found inside T` | The target table is missing a column | Add the column, or check `BQ_TABLE` points where you think |
| Env var change in the console disappeared | The next deploy overwrote it | Change it in the workflow YAML instead |
| Changed a GitHub variable but nothing changed | Only `GCP_DEV_PROJECT_ID` and `GCP_PROD_PROJECT_ID` are read. Region, service name and secret name are hardcoded | Change it in the workflow YAML instead |
| Production deployed twice from one commit | The old Cloud Build trigger for `main` is still enabled | Disable it |
| CI passes but the deploy breaks | CI only runs unit tests — no GCP, no BigQuery | Test against development before promoting |

---

## 10. Rules worth keeping

- The branch decides the environment. Nothing deploys by hand.
- Never commit to `dev` or `main` directly — always a PR.
- Never put a secret value in git, in a workflow file, or in a commit message.
  Secret Manager and GitHub Secrets are the only two places.
- Table and dataset names live in the workflow YAML, not the Cloud Run console.
- Every change reaches production through development first.
- If production breaks, roll back first, then investigate.
- Protect `main` with a required review. It is the only thing standing between a
  typo and a live incident.
