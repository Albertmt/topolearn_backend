"""All SQL lives here. Every query is index-backed and bounded."""
import re
import sqlite3
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .db import has_fts

_CONCEPT_COLS = "pk, slug, name, type, domain, description, example, wiki_url, difficulty, prereq_count"
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_CHUNK = 900  # stay under SQLite's host-parameter limit on old builds


def normalize(text: str) -> str:
    return " ".join(text.lower().split())


def _concept(row: sqlite3.Row) -> dict:
    return {
        "id": row["slug"],
        "name": row["name"],
        "type": row["type"],
        "domain": row["domain"],
        "description": row["description"],
        "example": row["example"],
        "wiki_url": row["wiki_url"],
        "difficulty": row["difficulty"],
        "prereq_count": row["prereq_count"],
    }


def _chunks(items: List[int]) -> Iterable[List[int]]:
    for i in range(0, len(items), _CHUNK):
        yield items[i : i + _CHUNK]


def count_concepts(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM concepts").fetchone()[0]


def get_concept_row(conn: sqlite3.Connection, slug: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        f"SELECT {_CONCEPT_COLS} FROM concepts WHERE slug = ?", (slug,)
    ).fetchone()


def get_concept(conn: sqlite3.Connection, slug: str) -> Optional[dict]:
    row = get_concept_row(conn, slug)
    return _concept(row) if row else None


# --------------------------------------------------------------------------- search


def search(conn: sqlite3.Connection, query: str, limit: int) -> List[dict]:
    q = normalize(query)
    if not q:
        return []

    # pk -> (bucket, score); lower is better.
    ranked: Dict[int, Tuple[int, float]] = {}

    def offer(pk: int, bucket: int, score: float = 0.0) -> None:
        if pk not in ranked or (bucket, score) < ranked[pk]:
            ranked[pk] = (bucket, score)

    # 1) exact name / alias
    for (pk,) in conn.execute("SELECT pk FROM concepts WHERE name_norm = ?", (q,)):
        offer(pk, 0)
    for (pk,) in conn.execute("SELECT concept_pk FROM aliases WHERE alias_norm = ?", (q,)):
        offer(pk, 0)

    # 2) prefix / full-text.
    # Concept pks are assigned in popularity order at build time, and FTS5 yields
    # rows in rowid order, so "LIMIT n" returns the n most popular matches
    # without scoring every hit (keeps 1-letter prefixes fast at 1M+ rows).
    candidates = limit * 2
    if has_fts(conn):
        tokens = _TOKEN_RE.findall(q)
        if tokens:
            starts_with = f'name : ^"{" ".join(tokens)}"*'
            anywhere = " ".join(f'"{t}"' for t in tokens[:-1])
            anywhere = (anywhere + f' "{tokens[-1]}"*').strip()
            for bucket, match in ((1, starts_with), (2, anywhere)):
                rows = conn.execute(
                    "SELECT rowid FROM concepts_fts WHERE concepts_fts MATCH ? LIMIT ?",
                    (match, candidates),
                )
                for (pk,) in rows:
                    offer(pk, bucket, float(pk))
    else:
        like = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = conn.execute(
            """
            SELECT pk, name_norm FROM concepts
            WHERE name_norm LIKE ? ESCAPE '\\'
            ORDER BY popularity DESC, length(name) LIMIT ?
            """,
            (f"%{like}%", candidates),
        )
        for pk, name_norm in rows:
            offer(pk, 1 if name_norm.startswith(q) else 2, float(len(name_norm)))

    if not ranked:
        return []
    best = sorted(ranked, key=lambda pk: ranked[pk])[:limit]
    rows_by_pk = {
        r["pk"]: r
        for r in conn.execute(
            f"SELECT {_CONCEPT_COLS} FROM concepts WHERE pk IN ({','.join('?' * len(best))})",
            best,
        )
    }
    return [_concept(rows_by_pk[pk]) for pk in best if pk in rows_by_pk]


# ---------------------------------------------------------------------- prerequisites


def _pks_for(conn: sqlite3.Connection, slugs: Iterable[str]) -> Set[int]:
    out: Set[int] = set()
    slugs = list(slugs)
    for i in range(0, len(slugs), _CHUNK):
        chunk = slugs[i : i + _CHUNK]
        out.update(
            r[0] for r in conn.execute(
                f"SELECT pk FROM concepts WHERE slug IN ({','.join('?' * len(chunk))})", chunk
            )
        )
    return out


def prerequisite_graph(
    conn: sqlite3.Connection,
    slug: str,
    max_depth: int,
    max_nodes: int,
    skip: Iterable[str] = (),
) -> Optional[dict]:
    """Breadth-first walk over prerequisites, bounded by depth and node count.

    Concepts in `skip` (a user's hidden concepts) are left out and not walked
    through, so their prerequisites only appear if something else needs them.
    The root is always shown.

    Work is O(max_nodes * avg_degree) no matter how big the whole graph is.
    """
    root = get_concept_row(conn, slug)
    if root is None:
        return None
    skip_pks = _pks_for(conn, skip) - {root["pk"]}
    skipped_out: Dict[int, int] = {}

    depth: Dict[int, int] = {root["pk"]: 0}
    edges: Dict[Tuple[int, int], Optional[str]] = {}
    frontier = [root["pk"]]
    truncated = False

    for level in range(1, max_depth + 1):
        if not frontier:
            break
        next_frontier: List[int] = []
        for chunk in _chunks(frontier):
            rows = conn.execute(
                f"""
                SELECT p.concept_pk, p.prereq_pk, p.why FROM prerequisites p
                JOIN concepts c ON c.pk = p.prereq_pk
                WHERE p.concept_pk IN ({','.join('?' * len(chunk))})
                ORDER BY p.concept_pk, c.popularity DESC, c.pk
                """,
                chunk,
            ).fetchall()
            for concept_pk, prereq_pk, why in rows:
                if prereq_pk in skip_pks:
                    skipped_out[concept_pk] = skipped_out.get(concept_pk, 0) + 1
                    continue
                if prereq_pk not in depth:
                    if len(depth) >= max_nodes:
                        truncated = True
                        continue
                    depth[prereq_pk] = level
                    next_frontier.append(prereq_pk)
                edges[(concept_pk, prereq_pk)] = why
        frontier = next_frontier
    # Nodes left unexpanded (depth limit) show up below via hidden_prereqs.

    pks = list(depth)
    rows: Dict[int, sqlite3.Row] = {}
    for chunk in _chunks(pks):
        for r in conn.execute(
            f"SELECT {_CONCEPT_COLS} FROM concepts WHERE pk IN ({','.join('?' * len(chunk))})",
            chunk,
        ):
            rows[r["pk"]] = r

    # Edges between nodes we include (also catches cross-links found at the last level).
    shown_out: Dict[int, int] = {}
    for a, _ in edges:
        shown_out[a] = shown_out.get(a, 0) + 1

    nodes = []
    for pk in sorted(pks, key=lambda p: (depth[p], rows[p]["name"].lower())):
        node = _concept(rows[pk])
        node["depth"] = depth[pk]
        node["hidden_prereqs"] = max(
            0, rows[pk]["prereq_count"] - shown_out.get(pk, 0) - skipped_out.get(pk, 0)
        )
        if node["hidden_prereqs"]:
            truncated = True
        nodes.append(node)

    return {
        "root": root["slug"],
        "nodes": nodes,
        "edges": [
            {"concept": rows[a]["slug"], "requires": rows[b]["slug"], "why": why}
            for (a, b), why in sorted(edges.items())
        ],
        "truncated": truncated,
    }


def unlocks(conn: sqlite3.Connection, slug: str, limit: int) -> Optional[List[dict]]:
    """Concepts that list `slug` as a direct prerequisite."""
    root = get_concept_row(conn, slug)
    if root is None:
        return None
    rows = conn.execute(
        f"""
        SELECT {', '.join('c.' + c.strip() for c in _CONCEPT_COLS.split(','))}
        FROM prerequisites p JOIN concepts c ON c.pk = p.concept_pk
        WHERE p.prereq_pk = ?
        ORDER BY c.popularity DESC, c.name LIMIT ?
        """,
        (root["pk"], limit),
    )
    return [_concept(r) for r in rows]


# ------------------------------------------------------------ learning path


def minutes_to_learn(difficulty: Optional[float]) -> int:
    """Rough study time for one concept, from 20 min (trivial) to ~3 h (research level)."""
    d = 0.3 if difficulty is None else max(0.0, min(1.0, difficulty))
    return int(round((20 + 160 * d ** 1.5) / 5.0) * 5)


def direct_prereqs(conn: sqlite3.Connection, slug: str) -> List[str]:
    return [r[0] for r in conn.execute(
        """SELECT c2.slug FROM concepts c1
           JOIN prerequisites p ON p.concept_pk = c1.pk
           JOIN concepts c2 ON c2.pk = p.prereq_pk
           WHERE c1.slug = ?""", (slug,))]


def direct_dependents(conn: sqlite3.Connection, slug: str, limit: int = 200) -> List[dict]:
    rows = conn.execute(
        f"""SELECT {', '.join('c.' + c.strip() for c in _CONCEPT_COLS.split(','))}
            FROM concepts p0 JOIN prerequisites p ON p.prereq_pk = p0.pk
            JOIN concepts c ON c.pk = p.concept_pk
            WHERE p0.slug = ? ORDER BY c.popularity DESC LIMIT ?""", (slug, limit))
    return [_concept(r) for r in rows]


def learning_path(
    conn: sqlite3.Connection, slug: str, skip: Iterable[str] = (), max_nodes: int = 1500
) -> Optional[dict]:
    """Every prerequisite of `slug` (whole closure, hidden ones pruned) in an
    order you could actually study them: each concept comes after all of its
    prerequisites; among concepts that are ready at the same time, easier first.
    The target itself is last."""
    root = get_concept_row(conn, slug)
    if root is None:
        return None
    skip_pks = _pks_for(conn, skip) - {root["pk"]}

    seen: Set[int] = {root["pk"]}
    edges: List[Tuple[int, int]] = []
    frontier = [root["pk"]]
    truncated = False
    while frontier:
        nxt: List[int] = []
        for chunk in _chunks(frontier):
            for a, b in conn.execute(
                f"SELECT concept_pk, prereq_pk FROM prerequisites WHERE concept_pk IN ({','.join('?' * len(chunk))})",
                chunk,
            ):
                if b in skip_pks:
                    continue
                if b not in seen:
                    if len(seen) >= max_nodes:
                        truncated = True
                        continue
                    seen.add(b)
                    nxt.append(b)
                edges.append((a, b))
        frontier = nxt

    rows: Dict[int, sqlite3.Row] = {}
    pks = list(seen)
    for chunk in _chunks(pks):
        for r in conn.execute(
            f"SELECT {_CONCEPT_COLS} FROM concepts WHERE pk IN ({','.join('?' * len(chunk))})", chunk
        ):
            rows[r["pk"]] = r

    # Kahn's algorithm over "prerequisite -> concept", easiest-first among ready ones.
    import heapq

    needs: Dict[int, Set[int]] = {pk: set() for pk in pks}
    users: Dict[int, List[int]] = {pk: [] for pk in pks}
    for a, b in edges:
        if a in needs and b in needs:
            needs[a].add(b)
            users[b].append(a)
    remaining = {pk: len(v) for pk, v in needs.items()}

    def key(pk: int):
        r = rows[pk]
        return (1 if pk == root["pk"] else 0, r["difficulty"] if r["difficulty"] is not None else 0.5,
                r["name"].lower(), pk)

    heap = [key(pk) for pk, n in remaining.items() if n == 0]
    heapq.heapify(heap)
    order: List[int] = []
    while heap:
        pk = heapq.heappop(heap)[-1]
        order.append(pk)
        for u in users[pk]:
            remaining[u] -= 1
            if remaining[u] == 0:
                heapq.heappush(heap, key(u))

    steps = []
    for pk in order:
        c = _concept(rows[pk])
        c["requires"] = sorted(rows[p]["slug"] for p in needs[pk])
        c["minutes"] = minutes_to_learn(c["difficulty"])
        steps.append(c)
    return {"root": root["slug"], "steps": steps, "truncated": truncated}
