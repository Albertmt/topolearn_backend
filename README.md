# topolearn_backend

API behind the Topolearn app: step-by-step courses for programming languages, libraries and tools, laid out as
dependency trees of small lessons, plus each learner's progress, reviews, reactions and comments.

**Stack:** FastAPI + **PostgreSQL** (hosted on [Neon](https://neon.com)). Everything lives in one database:

- **`graph` schema:** the catalog (tools, their lessons and the prerequisite links between lessons), loaded from
  `data/tools/*.json` by `scripts/build_db.py`.
- **`public` schema:** users, progress, enrolled tools, reviews, reactions, saved lessons, comments, reports and
  tool requests. These tables are created automatically on first use (`app/userdb.py`).

The server holds no state, so Cloud Run can start and stop copies freely.

## Setup

Connection strings and settings live in a git-ignored `.env` file next to this README:

```bash
# What the server and loader use locally: the Neon `dev` branch.
DATABASE_URL="postgresql://…"
# A database the tests may wipe: `topolearn_test` on the dev branch.
TOPOLEARN_TEST_DATABASE_URL="postgresql://…/topolearn_test?…"
# Production, pooled (the "-pooler" host): what Cloud Run uses.
PROD_DATABASE_URL="postgresql://…-pooler…"
# Production, direct: for loading the catalog.
PROD_DATABASE_URL_DIRECT="postgresql://…"
# Sign-in: the Firebase project whose ID tokens the server accepts.
FIREBASE_PROJECT_ID="topolearn-gcloud"
# Who may list problem reports and tool requests (verified email, comma-separated).
ADMIN_EMAILS="you@example.com"
# Email/password sign-up: the confirmation and password-reset emails go out
# through Resend (resend.com) from do-not-reply@topolearnapp.com. Without the
# key the links are only logged (fine locally).
RESEND_API_KEY="re_…"
# Where emailed links point (the website).
SITE_URL="https://topolearnapp.com"
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
python scripts/build_db.py                      # data/tools/*.json -> the DATABASE_URL database
uvicorn app.main:app --reload --host 0.0.0.0 --port 8001
```

- `--host 0.0.0.0` lets a phone on your Wi-Fi reach it. Open http://localhost:8001/docs for the interactive API.
- Reloading the catalog (`build_db.py`) is safe while the server runs. It keeps serving the old catalog until the
  new one is fully in place.

## Tests

```bash
pytest                                    # reads TOPOLEARN_TEST_DATABASE_URL from .env
python scripts/validate_content.py        # checks every curriculum in data/tools in detail
```

- **What runs:** about 100 tests against a real Postgres database, `TOPOLEARN_TEST_DATABASE_URL`, including one
  that loads the real curricula.
- **What they wipe:** before every test, the user tables in that database are dropped. Never point it at real
  data; the test setup refuses to run if it equals `DATABASE_URL`.
- **What they cover:** the catalog (tools, maps at each depth, cross-tool lessons, search, share page), sign-in
  (real token checks with a test key, and that nobody can reach another learner's data), enrolling and depth,
  progress and what it unlocks, "I already know this", reviews, stats, the feed, reactions, saved lessons,
  comments and replies, reports, tool requests, and the database layer (transactions, stale connections,
  reloads while serving).

## How the catalog works

- **Tool:** a language, library, framework or program (`python`, `react`, `docker`, …). It has modules (chapters),
  a colour and icon, a link to its official docs, and may build on other tools at a level ("React needs
  JavaScript, Practical").
- **Lesson:** one idea, 5–20 minutes: a summary, why it matters, an explanation (light Markdown), a code example,
  key points, pitfalls, a short quiz and a docs link. Its id is `<tool>.<slug>`, e.g. `python.list-comprehensions`.
- **Depth:** every lesson has a level: 1 Essentials, 2 Practical, 3 Advanced, 4 Expert. A learner picks how deep
  to go per tool; going to level *n* means every lesson of level ≤ *n*.
- **Prerequisites:** lessons list the lessons they directly build on, which may belong to another tool
  (`react.state` needs `javascript.closures`). A lesson never needs a deeper lesson of its own tool, so every
  depth is self-contained. Loops are rejected.
- **The map:** a tool's lessons down to a depth form a tree (a DAG) with the tool on top and the foundations at
  the bottom. Lessons from other tools that it builds on are included and marked `external`. `order` gives a
  study order (foundations first, then by level and position).
- **Ready / done:** a lesson is done when learned (understanding 100) or skipped (status `hidden`), and ready when
  everything it needs is done.
- **Reviews:** a learned lesson comes back for a quiz question one day later; a right answer multiplies the gap
  (×3 the first time, then ×2.3, up to 180 days), a wrong one resets it to a day.

## API

All paths are under `/api/v1` except `/health` and the share page.

| Endpoint | What it does |
|---|---|
| `GET /health` | status, tool and lesson counts |
| `GET /categories`, `GET /tools?category=` | the catalog: tools with lesson counts, time, learners, per-level totals and `logo_svg` |
| `GET /collections` | the catalog grouped into topics for browsing, each with its tools as cards |
| `GET /tools/{tool}?user_id=` | one tool: description, modules, levels, what it builds on (with the learner's progress there) and what builds on it; with `user_id`, whether they're learning it and their progress |
| `GET /tools/{tool}/map?depth=&user_id=` | the dependency tree down to `depth` (default: the learner's depth, else 2): `nodes` (with the learner's marks and `saved`), `edges` (`concept` needs `requires`), `order`, `progress` |
| `GET /lessons/{lesson}?user_id=` | a whole lesson, plus what it needs and unlocks, the next lesson, reactions, comment count, how many learned it, and whether the learner is ready for it |
| `GET /search?q=&tool=&limit=` | tools (names and aliases) and lessons; `tool` limits lessons to one tool |
| `GET /t/{tool}?depth=` | public HTML page with a tool's curriculum (for sharing) |
| `POST /session` | the signed-in learner's account; created on their first sign-in |
| `GET/PATCH/DELETE /users/{id}` | the learner's account; PATCH any of `name, email, experience (new/some/pro), daily_goal (1–20), onboarded`. DELETE also removes their Firebase sign-in |
| `GET /users/{id}/tools` | the tools they're learning, each with progress and the next lessons |
| `PUT/DELETE /users/{id}/tools/{tool}` | start learning a tool or change its `depth`; stop learning it (progress is kept) |
| `GET /users/{id}/tools/{tool}/next` | what to learn next in a tool, with a reason (blocked lessons point at the other tool's lesson they need) |
| `POST /users/{id}/tools/{tool}/known` | `{level}`: "I already know this tool up to Practical" marks those lessons learned |
| `PUT/DELETE /users/{id}/progress/{lesson}` | `understanding` 0–100 and/or `status` (`need_to_learn`, `hidden` = skipped, or null). PUT answers with `newly_ready` lessons and `finished_tool` when the learner just completed a tool at their depth |
| `POST /users/{id}/progress/bulk` | `{lesson_ids, understanding?, status?}`: "I know this and everything below it" |
| `GET /users/{id}/progress?status=&tool=` | the learner's marks (e.g. everything they skipped) |
| `GET /users/{id}/reviews`, `POST /users/{id}/reviews/{lesson}` | lessons due for review with their quiz; record `{correct}` |
| `GET /users/{id}/feed?tz_offset_minutes=` | home: today's goal and streak, tools in progress, up next, reviews due, saved lessons, trending tools |
| `GET /users/{id}/stats?tz_offset_minutes=` | counts, minutes learned, streaks, per week and per day, per tool |
| `PUT /lessons/{lesson}/reaction` | `{user_id, value: 1 / -1 / 0}` thumbs up/down |
| `GET /users/{id}/saved`, `PUT/DELETE /users/{id}/saved/{lesson}` | bookmarked lessons |
| `GET/POST /lessons/{lesson}/comments` | discussion (`sort=top|new`, replies one level deep); POST `{user_id, body, parent_id?}` |
| `PATCH/DELETE /comments/{id}`, `PUT /comments/{id}/like` | edit or delete your own comment; like someone else's |
| `POST /reports`, `GET /reports`, `GET /report-kinds` | flag a wrong/outdated lesson, a missing or unneeded prerequisite, a broken link, a typo or a comment; listing is for `ADMIN_EMAILS` only |
| `GET /tool-requests/lookup?name=`, `POST /tool-requests`, `GET /tool-requests` | ask for a tool that isn't in the catalog yet (one vote per person); the ranked list is for `ADMIN_EMAILS` only |

**Sign-in.** Every request that names a learner (`user_id` in the path, the query or the body) must carry that
learner's Firebase ID token, `Authorization: Bearer <token>`; otherwise it gets 401 (not signed in, or the token
is invalid or expired) or 403 (signed in as someone else). The catalog, lessons, search, comments and the share
page are public.

## Users

- **What's stored:** learners, their marks, enrolled tools and depths, review schedule, reactions, saved lessons,
  comments and likes, reports and tool requests, in the `public` schema (`app/userdb.py`, versioned by
  `SCHEMA_VERSION`).
- **References to lessons:** these tables store lesson ids (`python.loops`) as plain text rather than linking to
  the catalog tables, so reloading the catalog never breaks user data. Ids that disappear in a reload are skipped
  when shown.
- **Deleting a learner:** removes everything above that belongs to them, including their comments.
- **Sign-in:** the app signs in with Google or Facebook through Firebase Authentication and sends the Firebase
  ID token. `app/auth.py` checks its signature against Google's public keys (no Firebase SDK needed) and maps the
  Firebase uid to the learner.

## Data

One JSON file per tool in `data/tools/` (format and writing guide: `data/tools/AUTHORING.md`; the full list
of tool ids: `data/tools/CATALOG_PLAN.md`). About 155 tools and 3,200 lessons: programming languages,
Python's standard library and its data/ML/web libraries, web frameworks, databases, AWS/Azure/GCP, shells and
Linux, DevOps tooling, mobile. Two more files shape how the catalog is browsed:

- `data/collections.json`: the topic groups the app shows ("Programming languages", "Python for data & ML",
  "Databases & data", …), each a curated, ordered list of tool ids. A tool may be in several; tools in none are
  listed last under "More tools". The order of first appearance is the catalog order.
- `data/logos.json`: a 24x24 single-path SVG per tool that has a logo (from Simple Icons, CC0); the app draws it in
  white on the tool's colour. Tools without one show their monogram. See `data/LOGOS.md`.

```bash
python scripts/validate_content.py data/tools/rust.json   # one file, or none for all
python scripts/build_db.py                                # load everything into DATABASE_URL
```

The load checks that every prerequisite exists (unknown ones are dropped with a warning), that no lesson needs a
deeper lesson of its own tool, and that there are no loops, even across tools. It writes everything into a new
schema and swaps it in for the live one in one transaction, so the API never sees a half-loaded catalog and a
failed load leaves the old one in place. User data is never touched.

**Adding a tool:** write `data/tools/<tool>.json` following AUTHORING.md, add its id to the right group(s) in
`data/collections.json` (that decides where it shows and the catalog order), optionally a logo in `data/logos.json`,
validate, load. No app release is needed: the app
reads the catalog from the API.

## Deploy: Neon + Google Cloud Run

### 1. Load the catalog into production (from your Mac)

```bash
source .venv/bin/activate
set -a; source .env; set +a
python scripts/build_db.py --database-url "$PROD_DATABASE_URL_DIRECT"
```

The direct (non-pooled) string is a little faster for loading, but either works. Run this again whenever
anything in `data/tools` changes; no redeploy is needed. Without `--database-url`, the loader uses `DATABASE_URL`
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
  --set-secrets DATABASE_URL=database-url:latest,RESEND_API_KEY=resend-api-key:latest \
  --set-env-vars "FIREBASE_PROJECT_ID=topolearn-gcloud,ADMIN_EMAILS=you@example.com,SITE_URL=https://topolearnapp.com"
```

(`resend-api-key` is a Secret Manager secret like `database-url`; create it once with
`printf '%s' "re_…" | gcloud secrets create resend-api-key --data-file=-` and grant the
same `secretAccessor` binding as above.)

Once, so that deleting an account also deletes the Firebase sign-in record, and so the server
can mint the sign-in / password-reset links it emails:

```bash
gcloud projects add-iam-policy-binding topolearn-gcloud \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role=roles/firebaseauth.admin
```

- The first deploy asks to create an Artifact Registry repository. Answer **Y**.
- **Region:** `us-west1` is Oregon, the same area as the Neon database (AWS us-west-2), so queries stay local.
- **Output:** it prints the service URL, `https://topolearn-api-….run.app`.

Check it with `curl https://topolearn-api-….run.app/health`, which should return
something like `{"status":"ok","tools":155,"lessons":3100}`.

Tips:
- **Logs:** `gcloud run services logs read topolearn-api --region us-west1`.
- **Cold starts:** an idle server scales to zero, and Neon suspends after 5 idle minutes. The first request after
  a quiet period takes a second or two. A request that lands exactly as the database wakes can get a 503;
  trying again works.
- **Changing the database password:** `printf %s "$NEW_URL" | gcloud secrets versions add database-url --data-file=-`,
  then redeploy.

## Security notes

- **Sign-in:** only Firebase ID tokens for `FIREBASE_PROJECT_ID` are accepted; a learner can only read and change
  their own data. Listing problem reports needs a verified email in `ADMIN_EMAILS`.
- **Old password:** an older Neo4j version of this backend had its database password hard-coded, and that
  password is still in this repo's git history on GitHub. **Change it** if that database still exists.
