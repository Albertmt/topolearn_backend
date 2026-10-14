# topolearn_backend

Read-only API that serves concept prerequisite graphs for the Topolearn app.

**Stack:** FastAPI + a single SQLite file (`data/topolearn.db`) built offline from JSON.
No database server, nothing to connect to, so it starts instantly. It scales out by running more copies.

## Run locally

```bash
cd ~/Documents/src/topolearn_backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python scripts/build_db.py                      # data/concepts.json -> data/topolearn.db
uvicorn app.main:app --reload --host 0.0.0.0 --port 8001
```

`--host 0.0.0.0` lets a phone on your Wi-Fi reach it. Open http://localhost:8001/docs for the interactive API.

After rebuilding the DB you don't need to restart: the server notices the new file.

Tests: `pytest` — about 80 tests, including an edge-case suite (`tests/test_edge_cases.py`: huge, very wide,
very tall and single-node graphs, sparse data, odd text, stale ids after a rebuild, missing DB).

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

Learners, their progress marks, and community learning sources/votes live in a separate, writable
`data/users.db` (created on first use; path via `TOPOLEARN_USERS_DB`; schema in `app/userdb.py`).
Deleting a learner removes their marks and votes; links they added stay for everyone else.
Login is a placeholder for now: the app lists users and you pick one. The `auth_provider` and `auth_subject`
columns are there for Google sign-in later. On Cloud Run the disk is temporary, so before launch move users
to a hosted DB (Firestore, Supabase or Turso) behind the same functions in `app/users.py`.

## Data

`scripts/build_db.py` takes one or more JSON files:

```json
{
  "concepts": [{"id": "linear_regression", "name": "Linear Regression", "type": "algorithm",
                "description": "…", "wiki_url": "…", "difficulty": 0.3, "aliases": ["OLS"]}],
  "prerequisites": [{"concept": "linear_regression", "requires": "gradient_descent"}]
}
```

The build rejects prerequisite cycles and drops edges that point at unknown ids.

To fold duplicate concepts together, add `<keep> <drop>` lines to `data/merges.txt` and run
`python scripts/merge_concepts.py`, then `python scripts/build_db.py`. It re-points edges, breaks any cycles the
merge created, removes redundant links and re-smooths difficulty. Re-running it is safe.

`data/concepts.json` holds about 4,920 concepts across math, statistics, optimization, CS algorithms, data
engineering, data science, classical ML, deep learning, NLP/LLMs, CV/speech/generative, RL/probabilistic ML,
classical AI, and ML theory/trustworthy AI/graph ML. It has about 9,300 direct-prerequisite edges, with
transitively redundant edges removed. Each concept has a description, a worked example/analogy, a difficulty (0–1) and a domain; every edge has a one-line
`why` (what the prerequisite is used for).

Measured on a synthetic 1M-concept / 3M-edge graph: DB is 414 MB, built in about 2.5 min.
Graph queries take 4 ms p50 (150 nodes) and 10 ms p50 (400 nodes). Search takes under 1 ms p50 and 25 ms p95.

## Deploy (free)

The DB is baked into the image (`Dockerfile`). Recommended host is **Google Cloud Run**. Its always-free tier gives
2M requests, 180k vCPU-seconds and 360k GiB-seconds per month, and it scales to zero.

```bash
gcloud run deploy topolearn-api --source . --region us-central1 \
  --allow-unauthenticated --memory 1Gi --max-instances 3
```

Set `--max-instances` so a traffic spike can't run past the free tier.

## Security note

An older Neo4j version of this backend had its database password hard-coded, and that password is still
in this repo's git history on GitHub, so **change that password** if the database still exists.
