#!/usr/bin/env python3
"""Build data/topolearn.db from JSON source files.

Input format (one or more files, merged in order):

    {
      "concepts": [
        {"id": "linear_regression", "name": "Linear Regression",
         "type": "algorithm", "description": "...", "wiki_url": "...",
         "difficulty": 0.3, "aliases": ["OLS"], "popularity": 12.5}
      ],
      "prerequisites": [
        {"concept": "linear_regression", "requires": "gradient_descent"}
      ]
    }

Only "id" and "name" are required. The build checks that every edge points at
a known concept and that the graph has no cycles (a prerequisite loop would
mean nothing can be learned first). The DB is written to a temp file and
swapped in atomically, so a running server never sees a half-built file.

Usage:
    python scripts/build_db.py                      # data/concepts.json -> data/topolearn.db
    python scripts/build_db.py data/a.json data/b.json --out data/topolearn.db
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.repository import normalize  # noqa: E402

SCHEMA = (ROOT / "app" / "schema.sql").read_text()


Edges = Dict[Tuple[str, str], Optional[str]]  # (concept, requires) -> why


def load(paths: List[Path]) -> Tuple[Dict[str, dict], Edges]:
    concepts: Dict[str, dict] = {}
    edges: Edges = {}
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        for c in data.get("concepts", []):
            cid, name = str(c.get("id", "")).strip(), str(c.get("name", "")).strip()
            if not cid or not name:
                print(f"  ! {path.name}: skipping concept without id/name: {c}")
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


def build(concepts: Dict[str, dict], edges: Edges, out: Path) -> None:
    tmp = out.with_suffix(".db.tmp")
    if tmp.exists():
        tmp.unlink()
    conn = sqlite3.connect(tmp)
    conn.executescript(SCHEMA)

    dependents: Dict[str, int] = defaultdict(int)
    prereqs: Dict[str, int] = defaultdict(int)
    for a, b in edges:
        dependents[b] += 1
        prereqs[a] += 1

    def popularity(cid: str) -> float:
        return float(concepts[cid].get("popularity", dependents[cid]))

    # Most popular concept gets pk 1: search relies on this ordering.
    order = sorted(concepts, key=lambda cid: (-popularity(cid), cid))
    pk = {cid: i for i, cid in enumerate(order, start=1)}
    conn.executemany(
        """INSERT INTO concepts
           (pk, slug, name, name_norm, type, domain, description, example, wiki_url, difficulty,
            popularity, prereq_count)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            (
                pk[cid], cid, c["name"], normalize(c["name"]), c.get("type"), c.get("domain"),
                c.get("description"), c.get("example"), c.get("wiki_url") or c.get("wiki"),
                c.get("difficulty"),
                popularity(cid), prereqs[cid],
            )
            for cid, c in concepts.items()
        ),
    )
    conn.executemany(
        "INSERT OR IGNORE INTO aliases (concept_pk, alias, alias_norm) VALUES (?,?,?)",
        (
            (pk[cid], a, normalize(a))
            for cid, c in concepts.items()
            for a in c.get("aliases", []) or []
            if normalize(a)
        ),
    )
    conn.executemany(
        "INSERT INTO prerequisites (concept_pk, prereq_pk, why) VALUES (?,?,?)",
        ((pk[a], pk[b], why) for (a, b), why in edges.items()),
    )

    try:
        conn.execute(
            """CREATE VIRTUAL TABLE concepts_fts USING fts5(
                   name, aliases, content='', tokenize='unicode61 remove_diacritics 2',
                   prefix='1 2 3')"""
        )
        conn.executemany(
            "INSERT INTO concepts_fts (rowid, name, aliases) VALUES (?,?,?)",
            (
                (pk[cid], c["name"], " ".join(c.get("aliases", []) or []))
                for cid, c in concepts.items()
            ),
        )
        conn.execute("INSERT INTO concepts_fts(concepts_fts) VALUES ('optimize')")
        search = "fts5"
    except sqlite3.OperationalError as exc:
        print(f"  ! FTS5 unavailable ({exc}); search falls back to LIKE")
        search = "like"

    conn.executemany(
        "INSERT INTO meta (key, value) VALUES (?,?)",
        [
            ("built_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
            ("concepts", str(len(concepts))),
            ("prerequisites", str(len(edges))),
            ("search", search),
        ],
    )
    conn.commit()
    conn.execute("ANALYZE")
    conn.execute("VACUUM")
    conn.close()
    os.replace(tmp, out)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("sources", nargs="*", type=Path, default=[ROOT / "data" / "concepts.json"])
    p.add_argument("--out", type=Path, default=ROOT / "data" / "topolearn.db")
    args = p.parse_args()

    t0 = time.time()
    concepts, edges = load(args.sources)
    edges = validate(concepts, edges)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    build(concepts, edges, args.out)
    size_mb = args.out.stat().st_size / 1e6
    print(
        f"Built {args.out} — {len(concepts)} concepts, {len(edges)} prerequisites, "
        f"{size_mb:.1f} MB in {time.time() - t0:.1f}s"
    )


if __name__ == "__main__":
    main()
