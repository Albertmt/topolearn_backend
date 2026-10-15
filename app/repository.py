"""All concept-graph SQL lives here. Every query is index-backed and bounded."""
import heapq
import re
import unicodedata
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .db import Conn, Row

_COLS = ["pk", "slug", "name", "type", "domain", "description", "example", "wiki_url", "difficulty", "prereq_count"]
_CONCEPT_COLS = ", ".join(_COLS)
_C_COLS = ", ".join("c." + c for c in _COLS)
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def normalize(text: str) -> str:
    """Lower-case, single-spaced (exact name/alias matching)."""
    return " ".join(text.lower().split())


# Letters that don't decompose into "base letter + accent".
_SPECIAL = str.maketrans({"ß": "ss", "œ": "oe", "æ": "ae", "ø": "o", "ł": "l", "đ": "d", "ı": "i", "þ": "th"})


def fold(text: str) -> str:
    """Lower-case and strip accents: "Gödel" -> "godel", "Straße" -> "strasse"."""
    decomposed = unicodedata.normalize("NFKD", text.lower().translate(_SPECIAL))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def words(text: str) -> List[str]:
    """Accent-free words, the unit search matches on."""
    return _WORD.findall(fold(text))


def search_words(text: str) -> List[str]:
    """`words`, plus joined forms of punctuated words, so "tsne" finds "t-SNE"
    and "godels" finds "Gödel's"."""
    out = words(text)
    for chunk in fold(text).split():
        parts = _WORD.findall(chunk)
        if len(parts) > 1:
            out.append("".join(parts))
    return out


def _concept(row: Row) -> dict:
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


def count_concepts(conn: Conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM graph.concepts").fetchone()[0]


def get_concept_row(conn: Conn, slug: str) -> Optional[Row]:
    return conn.execute(f"SELECT {_CONCEPT_COLS} FROM graph.concepts WHERE slug = ?", (slug,)).fetchone()


def get_concept(conn: Conn, slug: str) -> Optional[dict]:
    row = get_concept_row(conn, slug)
    return _concept(row) if row else None


def concepts_by_slug(conn: Conn, slugs: Iterable[str]) -> Dict[str, dict]:
    """Several concepts in one query: {slug: concept} (unknown slugs are left out)."""
    wanted = list(dict.fromkeys(slugs))
    if not wanted:
        return {}
    rows = conn.execute(f"SELECT {_CONCEPT_COLS} FROM graph.concepts WHERE slug = ANY(?)", (wanted,))
    return {r["slug"]: _concept(r) for r in rows}


def _rows_by_pk(conn: Conn, pks: List[int]) -> Dict[int, Row]:
    if not pks:
        return {}
    return {r["pk"]: r for r in conn.execute(f"SELECT {_CONCEPT_COLS} FROM graph.concepts WHERE pk = ANY(?)", (pks,))}


# --------------------------------------------------------------------------- search


def _like_prefix(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def search(conn: Conn, query: str, limit: int) -> List[dict]:
    """Ranked: exact name/alias, then names starting with the query, then names
    or aliases containing all its words (the last one as a prefix). Within a
    bucket the more popular concept wins (pk order). Accents and case are ignored."""
    q = normalize(query)
    if not q:
        return []
    ranked: Dict[int, Tuple[int, int]] = {}  # pk -> (bucket, pk); lower is better

    def offer(pk: int, bucket: int) -> None:
        if pk not in ranked or (bucket, pk) < ranked[pk]:
            ranked[pk] = (bucket, pk)

    # 1) exact name / alias
    for (pk,) in conn.execute(
        """SELECT pk FROM graph.concepts WHERE name_norm = ?
           UNION SELECT concept_pk FROM graph.aliases WHERE alias_norm = ?""",
        (q, q),
    ):
        offer(pk, 0)

    tokens = words(q)
    if tokens:
        candidates = limit * 2
        # 2) the name starts with the query
        for (pk,) in conn.execute(
            "SELECT pk FROM graph.concepts WHERE name_key LIKE ? ORDER BY pk LIMIT ?",
            (_like_prefix(" ".join(tokens)), candidates),
        ):
            offer(pk, 1)
        # 3) every word appears somewhere in the name or an alias
        tsquery = " & ".join([f"'{t}'" for t in tokens[:-1]] + [f"'{tokens[-1]}':*"])
        for (pk,) in conn.execute(
            "SELECT pk FROM graph.concepts WHERE to_tsvector('simple', search_doc) @@ to_tsquery('simple', ?) "
            "ORDER BY pk LIMIT ?",
            (tsquery, candidates),
        ):
            offer(pk, 2)

    if not ranked:
        return []
    best = sorted(ranked, key=lambda pk: ranked[pk])[:limit]
    rows = _rows_by_pk(conn, best)  # may miss pks if the graph was reloaded meanwhile
    return [_concept(rows[pk]) for pk in best if pk in rows]


# ---------------------------------------------------------------------- prerequisites


def _pks_for(conn: Conn, slugs: Iterable[str]) -> Set[int]:
    slugs = list(slugs)
    if not slugs:
        return set()
    return {r[0] for r in conn.execute("SELECT pk FROM graph.concepts WHERE slug = ANY(?)", (slugs,))}


def prerequisite_graph(
    conn: Conn,
    slug: str,
    max_depth: int,
    max_nodes: int,
    skip: Iterable[str] = (),
) -> Optional[dict]:
    """Breadth-first walk over prerequisites, bounded by depth and node count
    (one query per level).

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
        rows = conn.execute(
            """SELECT p.concept_pk, p.prereq_pk, p.why FROM graph.prerequisites p
               WHERE p.concept_pk = ANY(?)
               ORDER BY p.concept_pk, p.prereq_pk""",  # prereq pk order = popularity order
            (frontier,),
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

    rows_by_pk = _rows_by_pk(conn, list(depth))
    if len(rows_by_pk) != len(depth):
        # The graph was reloaded between our queries (pks are reassigned on each
        # load): keep what's consistent rather than failing the request.
        depth = {pk: d for pk, d in depth.items() if pk in rows_by_pk}
        edges = {(a, b): w for (a, b), w in edges.items() if a in rows_by_pk and b in rows_by_pk}
        if root["pk"] not in rows_by_pk:
            return None

    shown_out: Dict[int, int] = {}
    for a, _ in edges:
        shown_out[a] = shown_out.get(a, 0) + 1

    nodes = []
    for pk in sorted(depth, key=lambda p: (depth[p], rows_by_pk[p]["name"].lower())):
        node = _concept(rows_by_pk[pk])
        node["depth"] = depth[pk]
        node["hidden_prereqs"] = max(
            0, rows_by_pk[pk]["prereq_count"] - shown_out.get(pk, 0) - skipped_out.get(pk, 0)
        )
        if node["hidden_prereqs"]:
            truncated = True
        nodes.append(node)

    return {
        "root": root["slug"],
        "nodes": nodes,
        "edges": [
            {"concept": rows_by_pk[a]["slug"], "requires": rows_by_pk[b]["slug"], "why": why}
            for (a, b), why in sorted(edges.items())
        ],
        "truncated": truncated,
    }


def unlocks(conn: Conn, slug: str, limit: int) -> Optional[List[dict]]:
    """Concepts that list `slug` as a direct prerequisite."""
    root = get_concept_row(conn, slug)
    if root is None:
        return None
    rows = conn.execute(
        f"""SELECT {_C_COLS}
            FROM graph.prerequisites p JOIN graph.concepts c ON c.pk = p.concept_pk
            WHERE p.prereq_pk = ?
            ORDER BY c.popularity DESC, c.name LIMIT ?""",
        (root["pk"], limit),
    )
    return [_concept(r) for r in rows]


# ------------------------------------------------------------ learning path


def minutes_to_learn(difficulty: Optional[float]) -> int:
    """Rough study time for one concept, from 20 min (trivial) to ~3 h (research level)."""
    d = 0.3 if difficulty is None else max(0.0, min(1.0, difficulty))
    return int(round((20 + 160 * d ** 1.5) / 5.0) * 5)


def direct_prereqs_many(conn: Conn, slugs: Iterable[str]) -> Dict[str, List[str]]:
    """{slug: [its direct prerequisites]} for several concepts in one query."""
    wanted = list(dict.fromkeys(slugs))
    out: Dict[str, List[str]] = {s: [] for s in wanted}
    if not wanted:
        return out
    for a, b in conn.execute(
        """SELECT c1.slug, c2.slug FROM graph.concepts c1
           JOIN graph.prerequisites p ON p.concept_pk = c1.pk
           JOIN graph.concepts c2 ON c2.pk = p.prereq_pk
           WHERE c1.slug = ANY(?)""",
        (wanted,),
    ):
        out[a].append(b)
    return out


def direct_prereqs(conn: Conn, slug: str) -> List[str]:
    return direct_prereqs_many(conn, [slug])[slug]


def direct_dependents(conn: Conn, slug: str, limit: int = 200) -> List[dict]:
    rows = conn.execute(
        f"""SELECT {_C_COLS}
            FROM graph.concepts p0 JOIN graph.prerequisites p ON p.prereq_pk = p0.pk
            JOIN graph.concepts c ON c.pk = p.concept_pk
            WHERE p0.slug = ? ORDER BY c.popularity DESC, c.pk LIMIT ?""",
        (slug, limit),
    )
    return [_concept(r) for r in rows]


def learning_path(conn: Conn, slug: str, skip: Iterable[str] = (), max_nodes: int = 1500) -> Optional[dict]:
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
        for a, b in conn.execute(
            "SELECT concept_pk, prereq_pk FROM graph.prerequisites WHERE concept_pk = ANY(?) ORDER BY 1, 2",
            (frontier,),
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

    rows = _rows_by_pk(conn, list(seen))
    if root["pk"] not in rows:
        return None  # the graph was reloaded mid-request
    pks = [pk for pk in seen if pk in rows]

    # Kahn's algorithm over "prerequisite -> concept", easiest-first among ready ones.
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
