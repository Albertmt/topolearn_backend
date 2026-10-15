# topolearn_backend

API that serves concept prerequisite graphs, learning paths and per-user progress for the Topolearn app.

**Stack:** FastAPI + **PostgreSQL** (hosted on [Neon](https://neon.com)). Everything lives in one database:

- **`graph` schema:** concepts, prerequisites and aliases, loaded from `data/concepts.json` by
  `scripts/build_db.py`.
- **`public` schema:** users, progress, goals, sources, votes and reports. These tables are created automatically
  on first use (`app/userdb.py`).

The server holds no state, so Cloud Run can start and stop copies freely.

## Setup

Connection strings live in a git-ignored `.env` file next to this README:

```bash
# What the server and loader use locally: the Neon `dev` branch.
DATABASE_URL="postgresql://…"
# A database the tests may wipe: `topolearn_test` on the dev branch.
TOPOLEARN_TEST_DATABASE_URL="postgresql://…/topolearn_test?…"
# Production, pooled (the "-pooler" host): what Cloud Run uses.
PROD_DATABASE_URL="postgresql://…-pooler…"
# Production, direct: for loading concepts.
PROD_DATABASE_URL_DIRECT="postgresql://…"
```

Keep the values in **double quotes**: the connection strings contain `&`, which a shell would otherwise treat
as "run in the background" when you `source .env`.

`.env` is excluded from git and from what `gcloud` uploads, so the passwords stay on your machine.

Create the test database once (on the `dev` branch):

```bash
set -a; source .env; set +a
python -c "import os, psycopg; psycopg.connect(os.environ['DATABASE_URL'], autocommit=True).execute('CREATE DATABASE topolearn_test')"
```

## Run locally

```bash
cd ~/Documents/src/topolearn_backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python scripts/build_db.py                      # data/concepts.json -> the DATABASE_URL database
uvicorn app.main:app --reload --host 0.0.0.0 --port 8001
```

- `--host 0.0.0.0` lets a phone on your Wi-Fi reach it. Open http://localhost:8001/docs for the interactive API.
- Reloading the concepts (`build_db.py`) is safe while the server runs. It keeps serving the old graph until the
  new one is fully in place.

## Tests

```bash
pytest            # reads TOPOLEARN_TEST_DATABASE_URL from .env
```

- **What runs:** about 100 tests against a real Postgres database, `TOPOLEARN_TEST_DATABASE_URL`.
- **What they wipe:** before every test, the user tables in that database are dropped. Never point it at real
  data; the test setup refuses to run if it equals `DATABASE_URL`.
- **What they cover:**
  - The API, users, learning features, sources, reports and the feed.
  - Edge cases: huge, very wide, very tall and single-node graphs, sparse data, odd text and stale ids after a
    reload.
  - The database layer: transactions and savepoints, constraint errors, placeholders, dropped and stale
    connections, reloads while serving, a missing or unreachable database.

## API

| Endpoint | What it returns |
|---|---|
| `GET /health` | status + concept count |
| `GET /api/v1/search?q=lin&limit=10` | autocomplete: exact name/alias first, then names that start with the query, then any word match; ties by popularity |
| `GET /api/v1/concepts/{id}` | one concept |
| `GET /api/v1/concepts/{id}/graph?user_id=…` | prerequisite DAG: `nodes`, `edges` (`concept` requires `requires`), `truncated`; each node has `hidden_prereqs` so the app can offer "show more". With `user_id`: that learner's hidden concepts are pruned (their prerequisites appear only if something else still needs them) and each node carries the learner's `understanding`/`status` |
| `GET /api/v1/concepts/{id}/unlocks` | concepts that directly need this one |
| `GET/POST /api/v1/users` | list / create learners (`{name, email?}`) |
| `GET/PATCH/DELETE /api/v1/users/{id}` | one learner; PATCH any of `name, email, age, education_level, interests` (concept ids), `onboarded` |
| `GET /api/v1/disciplines?q=` | onboarding quick picks (no `q`) or search, fields of study first |
| `PUT/DELETE /api/v1/users/{id}/progress/{concept}` | a learner's mark: `understanding` 0–100 and/or `status` (`need_to_learn`, `hidden`, or null) |
| `GET /api/v1/users/{id}/progress?status=hidden` | the learner's marks (e.g. everything they hid) |
| `GET /api/v1/concepts/{id}/path?user_id=` | every prerequisite in study order (foundations first, target last), each marked done/ready, time estimates, and sources that cover several remaining steps |
| `GET/PUT/DELETE /api/v1/users/{id}/goals[/{concept}]` | pinned goals (max 5) with progress and the next ready steps |
| `GET /api/v1/users/{id}/feed` | home feed: pick up where you left off, ready next (from goals, then interests), recently mastered, popular this week |
| `GET /api/v1/users/{id}/stats?tz_offset_minutes=` | mastered/partly/need-to-learn counts, streaks, mastered per week (8 weeks) |
| `GET /api/v1/concepts/{id}/community` | learners studying it / who understand it / who pinned it, best source |
| `PUT /api/v1/sources/{id}/helped` | "this helped me understand it" (counts double when ranking) |
| `POST /api/v1/reports`, `GET /api/v1/reports` | learners flag wrong/missing prerequisites, bad descriptions/examples, duplicates; list for review |
| `GET /p/{id}` | public HTML page with the learning path to a concept (for sharing) |
| `GET/POST /api/v1/concepts/{id}/sources?kind=` | community learning links, best first; POST `{user_id, url, title?, kind, also_covers?: [ids]}` |
| `PUT /api/v1/sources/{id}/vote` | `{user_id, value: 1 / -1 / 0}` |
| `DELETE /api/v1/sources/{id}?user_id=` | remove a link (only the person who added it) |

Every graph request is bounded by `depth` (default 20, max 30) and `max_nodes` (default 200, max 500), so its cost doesn't grow with the size of the database.

## Users

- **What's stored:** learners, their progress marks, goals, and community learning sources, votes and reports, in
  the `public` schema (`app/userdb.py`).
- **References to concepts:** these tables store concept ids as plain text rather than linking to the concept
  tables, so reloading the concept graph never breaks user data. Ids that disappear in a reload are skipped when
  shown.
- **Deleting a learner:** removes their marks, goals and votes. Links they added stay for everyone else.
- **Login:** a placeholder for now; the app lists users and you pick one. The `auth_provider` and `auth_subject`
  columns are there for Google sign-in later.

## Data

`scripts/build_db.py` takes one or more JSON files:

```json
{
  "concepts": [{"id": "linear_regression", "name": "Linear Regression", "type": "algorithm",
                "description": "…", "wiki_url": "…", "difficulty": 0.3, "aliases": ["OLS"]}],
  "prerequisites": [{"concept": "linear_regression", "requires": "gradient_descent"}]
}
```

The load rejects prerequisite cycles and drops edges that point at unknown ids. It writes everything into a new
schema and swaps it in for the live one in one transaction, so the API never sees a half-loaded graph and a failed
load leaves the old data in place. User data is never touched.

To fold duplicate concepts together, add `<keep> <drop>` lines to `data/merges.txt` and run
`python scripts/merge_concepts.py`, then load again with `python scripts/build_db.py`. It re-points edges, breaks any cycles the
merge created, removes redundant links and re-smooths difficulty. Re-running it is safe.

`data/concepts.json` holds about 4,920 concepts across math, statistics, optimization, CS algorithms, data
engineering, data science, classical ML, deep learning, NLP/LLMs, CV/speech/generative, RL/probabilistic ML,
classical AI, and ML theory/trustworthy AI/graph ML. It has about 9,300 direct-prerequisite edges, with
transitively redundant edges removed. Each concept has a description, a worked example/analogy, a difficulty (0–1) and a domain; every edge has a one-line
`why` (what the prerequisite is used for).

### Scale

Measured on Postgres 16 with a synthetic graph of 1M concepts and 3M prerequisite links:

| | Result |
|---|---|
| Database size | ~1.2 GB |
| Load time | ~35 s |
| Search (median) | 1–4 ms; slowest measured 17 ms (several words, no match) |
| Map, 200 nodes | 5 ms |
| Map, 500 nodes | 11 ms |
| Full learning path (1,500-step cap) | 36 ms |

- **Neon's free plan:** 1 GB per project. A reload briefly holds two copies of the graph (the new one is built
  next to the live one, then swapped in), so on the free plan plan for about 400k concepts like these if you
  want to keep reloading. Above that, Neon's pay-as-you-go plan charges $0.35/GB-month, so the same project
  keeps working and nothing needs migrating.
- **Network:** each map loads level by level, one query per level. Keep the server and the database in the same
  region.

## Deploy: Neon + Google Cloud Run

### 1. Load the concepts into production (from your Mac)

```bash
source .venv/bin/activate
set -a; source .env; set +a
python scripts/build_db.py --database-url "$PROD_DATABASE_URL_DIRECT"
```

The direct (non-pooled) string is a little faster for loading, but either works. Run this again whenever
`data/concepts.json` changes; no redeploy is needed. Without `--database-url`, the loader uses `DATABASE_URL`
(your dev branch).

### 2. One-time Google Cloud setup

```bash
brew install --cask google-cloud-sdk
gcloud auth login
gcloud projects create topolearn-<something-unique>
gcloud config set project topolearn-<something-unique>
```

Then link a billing account to the project in the Console (Billing → Link a billing account). Cloud Run requires
this even on the free tier. Add a budget alert as well (Billing → Budgets & alerts, for example $1).

```bash
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com

# Store the production (pooled) connection string as a secret Cloud Run can read.
set -a; source .env; set +a
printf %s "$PROD_DATABASE_URL" | gcloud secrets create database-url --data-file=-
PROJECT_NUMBER=$(gcloud projects describe "$(gcloud config get-value project)" --format='value(projectNumber)')
gcloud secrets add-iam-policy-binding database-url \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role=roles/secretmanager.secretAccessor
```

### 3. Deploy (and again after every backend change)

```bash
gcloud run deploy topolearn-api --source . --region us-west1 \
  --allow-unauthenticated --memory 512Mi --max-instances 2 \
  --set-secrets DATABASE_URL=database-url:latest
```

- The first deploy asks to create an Artifact Registry repository. Answer **Y**.
- **Region:** `us-west1` is Oregon, the same area as the Neon database (AWS us-west-2), so queries stay local.
- **Output:** it prints the service URL, `https://topolearn-api-….run.app`.

Check it with `curl https://topolearn-api-….run.app/health`, which should return
`{"status":"ok","concepts":4921,"search":"postgres"}`.

Tips:
- **Logs:** `gcloud run services logs read topolearn-api --region us-west1`.
- **Cold starts:** an idle server scales to zero, and Neon suspends after 5 idle minutes. The first request after
  a quiet period takes a second or two. A request that lands exactly as the database wakes can get a 503;
  trying again works.
- **Changing the database password:** `printf %s "$NEW_URL" | gcloud secrets versions add database-url --data-file=-`,
  then redeploy.

## Security notes

- **Reports:** `GET /api/v1/reports` has no admin login yet. Add one before going public.
- **Old password:** an older Neo4j version of this backend had its database password hard-coded, and that
  password is still in this repo's git history on GitHub. **Change it** if that database still exists.
