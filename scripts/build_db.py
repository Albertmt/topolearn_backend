#!/usr/bin/env python3
"""Load the concept graph from JSON into Postgres (schema "graph").

Input format (one or more files, merged in order):

    {
      "concepts": [
        {"id": "linear_regression", "name": "Linear Regression",
         "type": "algorithm", "description": "...", "wiki_url": "...",
         "difficulty": 0.3, "aliases": ["OLS"], "popularity": 12.5}
      ],
      "prerequisites": [
        {"concept": "linear_regression", "requires": "gradient_descent", "why": "..."}
      ]
    }

Only "id" and "name" are required. The load checks that every edge points at
a known concept and that the graph has no cycles (a prerequisite loop would
mean nothing can be learned first).

Everything is written into a new schema first ("graph_build"), indexed, and
then swapped in for "graph" in one transaction: the API keeps serving the old
graph until the new one is complete, and never sees a half-loaded state.
User data (schema "public") is never touched.

Usage:
    python scripts/build_db.py                       # data/concepts.json -> $DATABASE_URL
    python scripts/build_db.py data/a.json data/b.json --database-url postgresql://...

Either Neon connection string works; the direct (non-pooled) one is a little faster.
"""
import argparse
import json
import sys
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402

from app import config  # noqa: E402
from app.repository import normalize, search_words, words  # noqa: E402

SCHEMA = (ROOT / "app" / "schema.sql").read_text()
LIVE, BUILD, OLD = "graph", "graph_build", "graph_old"
_LOAD_LOCK_KEY = 7_405_302  # advisory lock: one loader at a time


Edges = Dict[Tuple[str, str], Optional[str]]  # (concept, requires) -> why


def load(paths: List[Path]) -> Tuple[Dict[str, dict], Edges]:
    return _collect([json.loads(p.read_text(encoding="utf-8")) for p in paths])


def _collect(datas: List[dict]) -> Tuple[Dict[str, dict], Edges]:
    concepts: Dict[str, dict] = {}
    edges: Edges = {}
    for data in datas:
        for c in data.get("concepts", []):
            cid, name = str(c.get("id", "")).strip(), str(c.get("name", "")).strip()
            if not cid or not name:
                print(f"  ! skipping concept without id/name: {c}")
                continue
            concepts[cid] = {**concepts.get(cid, {}), **c, "id": cid, "name": name}
        for e in data.get("prerequisites", []):
            key = (str(e["concept"]).strip(), str(e["requires"]).strip())
            edges[key] = (e.get("why") or "").strip() or edges.get(key)
    return concepts, edges


def validate(concepts: Dict[str, dict], edges: Edges) -> Edges:
    good: Edges = {}
    for (a, b), why in sorted(edges.items()):
        if a == b:
            print(f"  ! self-loop dropped: {a}")
        elif a not in concepts or b not in concepts:
            missing = [x for x in (a, b) if x not in concepts]
            print(f"  ! edge {a} -> {b} dropped, unknown concept(s): {', '.join(missing)}")
        else:
            good[(a, b)] = why

    # Kahn's algorithm: whatever is left with in-degree > 0 sits on a cycle.
    indeg: Dict[str, int] = defaultdict(int)
    out: Dict[str, List[str]] = defaultdict(list)
    for a, b in good:
        out[a].append(b)
        indeg[b] += 1
    queue = deque(c for c in concepts if indeg[c] == 0)
    seen = 0
    while queue:
        n = queue.popleft()
        seen += 1
        for m in out[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
    if seen != len(concepts):
        stuck = sorted(c for c in concepts if indeg[c] > 0)
        raise SystemExit(
            f"Prerequisite cycle detected among {len(stuck)} concepts, e.g. {stuck[:10]}"
        )
    return good


def _schema_parts(schema: str):
    sql = SCHEMA.replace("{schema}", schema)
    before, after = sql.split("-- @after-load", 1)
    return before, after.split("\n", 1)[1]


def build(concepts: Dict[str, dict], edges: Edges, url: str) -> None:
    dependents: Dict[str, int] = defaultdict(int)
    prereqs: Dict[str, int] = defaultdict(int)
    for a, b in edges:
        dependents[b] += 1
        prereqs[a] += 1

    def popularity(cid: str) -> float:
        return float(concepts[cid].get("popularity", dependents[cid]))

    # Most popular concept gets pk 1: search and graph ordering rely on this.
    order = sorted(concepts, key=lambda cid: (-popularity(cid), cid))
    pk = {cid: i for i, cid in enumerate(order, start=1)}

    with psycopg.connect(url, prepare_threshold=None, connect_timeout=30) as conn:
        # One transaction for the whole load: if anything fails (or the process
        # dies), nothing is left behind and the live graph is untouched. A
        # transaction-scoped lock keeps two loads from running at once, and works
        # through Neon's pooled (PgBouncer) endpoint too.
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOAD_LOCK_KEY,))
            create, finish = _schema_parts(BUILD)
            conn.execute(f"DROP SCHEMA IF EXISTS {BUILD} CASCADE")
            conn.execute(f"CREATE SCHEMA {BUILD}")
            conn.execute(create)

            with conn.cursor() as cur:
                with cur.copy(
                    f"""COPY {BUILD}.concepts (pk, slug, name, name_norm, name_key, search_doc, type, domain,
                        description, example, wiki_url, difficulty, popularity, prereq_count) FROM STDIN"""
                ) as cp:
                    for cid in order:
                        c = concepts[cid]
                        aliases = [a for a in (c.get("aliases") or []) if str(a).strip()]
                        name_words = words(c["name"])
                        doc_words = search_words(c["name"]) + [w for a in aliases for w in search_words(str(a))]
                        cp.write_row((
                            pk[cid], cid, c["name"], normalize(c["name"]), " ".join(name_words),
                            " ".join(doc_words), c.get("type"), c.get("domain"), c.get("description"),
                            c.get("example"), c.get("wiki_url") or c.get("wiki"), c.get("difficulty"),
                            popularity(cid), prereqs[cid],
                        ))
                seen_alias = set()
                with cur.copy(f"COPY {BUILD}.aliases (concept_pk, alias, alias_norm) FROM STDIN") as cp:
                    for cid in order:
                        for a in concepts[cid].get("aliases") or []:
                            norm = normalize(str(a))
                            if norm and (norm, pk[cid]) not in seen_alias:
                                seen_alias.add((norm, pk[cid]))
                                cp.write_row((pk[cid], str(a), norm))
                with cur.copy(f"COPY {BUILD}.prerequisites (concept_pk, prereq_pk, why) FROM STDIN") as cp:
                    for (a, b), why in edges.items():
                        cp.write_row((pk[a], pk[b], why))
                cur.executemany(
                    f"INSERT INTO {BUILD}.meta (key, value) VALUES (%s, %s)",
                    [
                        ("built_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
                        ("concepts", str(len(concepts))),
                        ("prerequisites", str(len(edges))),
                    ],
                )
            conn.execute(finish)
            conn.execute(f"ANALYZE {BUILD}.concepts")
            conn.execute(f"ANALYZE {BUILD}.aliases")
            conn.execute(f"ANALYZE {BUILD}.prerequisites")

            # Swap at commit: readers see either the old graph or the new one.
            conn.execute(f"DROP SCHEMA IF EXISTS {OLD} CASCADE")
            exists = conn.execute(
                "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (LIVE,)
            ).fetchone()
            if exists:
                conn.execute(f"ALTER SCHEMA {LIVE} RENAME TO {OLD}")
            conn.execute(f"ALTER SCHEMA {BUILD} RENAME TO {LIVE}")
        # Only after the commit: dropping the old tables waits for queries still
        # reading them, and must not block anyone from reading the new graph.
        conn.execute(f"DROP SCHEMA IF EXISTS {OLD} CASCADE")
        conn.commit()


def load_into(url: str, data: dict) -> Tuple[int, int]:
    """Validate and load an in-memory {"concepts": [...], "prerequisites": [...]}.
    Returns (concepts, prerequisites) loaded. Used by the tests."""
    concepts, edges = _collect([data])
    edges = validate(concepts, edges)
    build(concepts, edges, url)
    return len(concepts), len(edges)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("sources", nargs="*", type=Path, default=[ROOT / "data" / "concepts.json"])
    p.add_argument("--database-url", default=None,
                   help="Postgres connection string (default: $DATABASE_URL, e.g. from .env)")
    args = p.parse_args()

    url = args.database_url or config.DATABASE_URL
    if not url:
        raise SystemExit("No database: pass --database-url or set DATABASE_URL (e.g. in .env).")

    t0 = time.time()
    concepts, edges = load(args.sources)
    edges = validate(concepts, edges)
    build(concepts, edges, url)
    print(f"Loaded {len(concepts)} concepts and {len(edges)} prerequisites in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
