"""All catalog SQL lives here: tools, lessons and their prerequisites (schema
"graph"). Every query is index-backed and bounded by one tool's size."""
import json
import re
import unicodedata
from typing import Dict, Iterable, List, Optional, Tuple

from .db import Conn, Row

LEVEL_NAMES = {1: "Essentials", 2: "Practical", 3: "Advanced", 4: "Expert"}
LEVEL_BLURBS = {
    1: "Read and write simple code, do the everyday basics",
    2: "Use it productively at work, with the usual idioms",
    3: "The less common features, performance and trade-offs",
    4: "Internals and the edge cases experts know",
}
MAX_LEVEL = 4

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
    """`words`, plus joined forms of punctuated words, so "nodejs" finds
    "Node.js" and "cpp"-style aliases work for "C++"-like names."""
    out = words(text)
    for chunk in fold(text).split():
        parts = _WORD.findall(chunk)
        if len(parts) > 1:
            out.append("".join(parts))
    return out


# ------------------------------------------------------------------------ tools

_TOOL_COLS = ("t.pk, t.slug, t.name, t.category, t.tagline, t.description, t.icon, t.color, "
              "t.official_docs, t.version, t.position, t.modules, t.logo_svg")


def _tool_card(row: Row) -> dict:
    return {
        "id": row["slug"],
        "name": row["name"],
        "category": row["category"],
        "tagline": row["tagline"],
        "icon": row["icon"],
        "color": row["color"],
        "logo_svg": row["logo_svg"],
    }


def _levels(stats: Dict[int, Tuple[int, int]]) -> List[dict]:
    """Per depth level: lessons and minutes at that level, and cumulative up to it."""
    out, lessons, minutes = [], 0, 0
    for lv in range(1, MAX_LEVEL + 1):
        n, m = stats.get(lv, (0, 0))
        lessons += n
        minutes += m
        out.append({
            "level": lv, "name": LEVEL_NAMES[lv], "blurb": LEVEL_BLURBS[lv],
            "lessons": n, "minutes": m, "total_lessons": lessons, "total_minutes": minutes,
        })
    return out


def _level_stats(conn: Conn, tool_pks: List[int]) -> Dict[int, Dict[int, Tuple[int, int]]]:
    out: Dict[int, Dict[int, Tuple[int, int]]] = {pk: {} for pk in tool_pks}
    if not tool_pks:
        return out
    for tool_pk, level, n, minutes in conn.execute(
        """SELECT tool_pk, level, COUNT(*), COALESCE(SUM(minutes), 0) FROM graph.concepts
           WHERE tool_pk = ANY(?) GROUP BY tool_pk, level""",
        (tool_pks,),
    ):
        out[tool_pk][level] = (n, int(minutes))
    return out


def count_tools(conn: Conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM graph.tools").fetchone()[0]


def count_lessons(conn: Conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM graph.concepts").fetchone()[0]


def collections(conn: Conn) -> List[dict]:
    """Curated groups of tools from data/collections.json, as stored by the loader."""
    row = conn.execute("SELECT value FROM graph.meta WHERE key = 'collections'").fetchone()
    if not row or not row[0]:
        return []
    try:
        return json.loads(row[0])
    except ValueError:
        return []


def list_tools(conn: Conn, category: Optional[str] = None) -> List[dict]:
    sql = f"SELECT {_TOOL_COLS} FROM graph.tools t"
    params: list = []
    if category:
        sql += " WHERE t.category = ?"
        params.append(category)
    rows = conn.execute(sql + " ORDER BY t.position", params).fetchall()
    stats = _level_stats(conn, [r["pk"] for r in rows])
    out = []
    for r in rows:
        card = _tool_card(r)
        levels = _levels(stats[r["pk"]])
        card["lessons"] = levels[-1]["total_lessons"]
        card["minutes"] = levels[-1]["total_minutes"]
        card["levels"] = levels
        out.append(card)
    return out


def get_tool_row(conn: Conn, slug: str) -> Optional[Row]:
    return conn.execute(f"SELECT {_TOOL_COLS} FROM graph.tools t WHERE t.slug = ?", (slug,)).fetchone()


def tools_by_slug(conn: Conn, slugs: Iterable[str]) -> Dict[str, dict]:
    wanted = list(dict.fromkeys(slugs))
    if not wanted:
        return {}
    rows = conn.execute(f"SELECT {_TOOL_COLS} FROM graph.tools t WHERE t.slug = ANY(?)", (wanted,))
    return {r["slug"]: _tool_card(r) for r in rows}


def get_tool(conn: Conn, slug: str) -> Optional[dict]:
    row = get_tool_row(conn, slug)
    if row is None:
        return None
    tool = _tool_card(row)
    levels = _levels(_level_stats(conn, [row["pk"]])[row["pk"]])
    tool.update({
        "description": row["description"],
        "official_docs": row["official_docs"],
        "version": row["version"],
        "modules": row["modules"] or [],
        "levels": levels,
        "lessons": levels[-1]["total_lessons"],
        "minutes": levels[-1]["total_minutes"],
        "requires": [
            {**_tool_card(r), "level": r["level"], "level_name": LEVEL_NAMES.get(r["level"], "")}
            for r in conn.execute(
                f"""SELECT {_TOOL_COLS}, q.level FROM graph.tool_requires q
                    JOIN graph.tools t ON t.pk = q.requires_pk WHERE q.tool_pk = ? ORDER BY t.position""",
                (row["pk"],),
            )
        ],
        "required_by": [
            _tool_card(r) for r in conn.execute(
                f"""SELECT {_TOOL_COLS} FROM graph.tool_requires q
                    JOIN graph.tools t ON t.pk = q.tool_pk WHERE q.requires_pk = ? ORDER BY t.position""",
                (row["pk"],),
            )
        ],
    })
    return tool


# ---------------------------------------------------------------------- lessons

_REF_COLS = ("c.pk, c.slug, c.name, c.level, c.minutes, c.summary, c.module, c.position, c.tool_pk, "
             "t.slug AS tool_slug, t.name AS tool_name, t.color AS tool_color, t.icon AS tool_icon")
_REF_FROM = "graph.concepts c JOIN graph.tools t ON t.pk = c.tool_pk"


def _ref(row: Row) -> dict:
    return {
        "id": row["slug"],
        "name": row["name"],
        "level": row["level"],
        "minutes": row["minutes"],
        "summary": row["summary"],
        "module": row["module"],
        "position": row["position"],
        "tool": {"id": row["tool_slug"], "name": row["tool_name"], "color": row["tool_color"],
                 "icon": row["tool_icon"]},
    }


def lessons_by_slug(conn: Conn, slugs: Iterable[str]) -> Dict[str, dict]:
    """Several lessons (short form) in one query: {slug: lesson}; unknown slugs left out."""
    wanted = list(dict.fromkeys(slugs))
    if not wanted:
        return {}
    rows = conn.execute(f"SELECT {_REF_COLS} FROM {_REF_FROM} WHERE c.slug = ANY(?)", (wanted,))
    return {r["slug"]: _ref(r) for r in rows}


def get_lesson(conn: Conn, slug: str) -> Optional[dict]:
    """The whole lesson: tutorial, code, quiz, and its neighbours in the tree."""
    row = conn.execute(
        f"""SELECT {_REF_COLS}, c.why, c.body, c.code, c.code_lang, c.takeaways, c.pitfalls, c.quiz,
                   c.docs_url, c.glossary, t.modules
            FROM {_REF_FROM} WHERE c.slug = ?""",
        (slug,),
    ).fetchone()
    if row is None:
        return None
    lesson = _ref(row)
    module_names = {m.get("id"): m.get("name") for m in (row["modules"] or [])}
    lesson.update({
        "module_name": module_names.get(row["module"], row["module"]),
        "level_name": LEVEL_NAMES.get(row["level"], ""),
        "why": row["why"],
        "body": row["body"],
        "code": row["code"],
        "code_lang": row["code_lang"],
        "takeaways": row["takeaways"] or [],
        "pitfalls": row["pitfalls"] or [],
        "quiz": row["quiz"] or [],
        "docs_url": row["docs_url"],
        "glossary": row["glossary"] or [],
        "requires": [
            _ref(r) for r in conn.execute(
                f"""SELECT {_REF_COLS} FROM graph.prerequisites p JOIN {_REF_FROM} ON c.pk = p.prereq_pk
                    WHERE p.concept_pk = ? ORDER BY c.tool_pk = ? DESC, c.position""",
                (row["pk"], row["tool_pk"]),
            )
        ],
        "unlocks": [
            _ref(r) for r in conn.execute(
                f"""SELECT {_REF_COLS} FROM graph.prerequisites p JOIN {_REF_FROM} ON c.pk = p.concept_pk
                    WHERE p.prereq_pk = ? ORDER BY c.tool_pk = ? DESC, c.level, c.position LIMIT 50""",
                (row["pk"], row["tool_pk"]),
            )
        ],
    })
    nxt = conn.execute(
        f"""SELECT {_REF_COLS} FROM {_REF_FROM}
            WHERE c.tool_pk = ? AND c.position > ? ORDER BY c.position LIMIT 1""",
        (row["tool_pk"], row["position"]),
    ).fetchone()
    lesson["next_in_tool"] = _ref(nxt) if nxt else None
    return lesson


def tool_lessons(conn: Conn, tool_slugs: Iterable[str]) -> Dict[str, List[dict]]:
    """{tool: [its lessons (short form), in study order]} for several tools."""
    wanted = list(dict.fromkeys(tool_slugs))
    out: Dict[str, List[dict]] = {s: [] for s in wanted}
    if not wanted:
        return out
    for r in conn.execute(
        f"SELECT {_REF_COLS} FROM {_REF_FROM} WHERE t.slug = ANY(?) ORDER BY c.tool_pk, c.position", (wanted,)
    ):
        out[r["tool_slug"]].append(_ref(r))
    return out


def requires_of(conn: Conn, lesson_slugs: Iterable[str]) -> Dict[str, List[str]]:
    """{lesson: [lessons it directly requires]} for several lessons."""
    wanted = list(dict.fromkeys(lesson_slugs))
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


def tool_map(conn: Conn, slug: str, depth: int) -> Optional[dict]:
    """The lessons of a tool down to a depth level, the prerequisite edges
    between them, and lessons of other tools they build on ("external")."""
    tool = get_tool(conn, slug)
    if tool is None:
        return None
    depth = max(1, min(MAX_LEVEL, depth))
    rows = conn.execute(
        f"SELECT {_REF_COLS} FROM {_REF_FROM} WHERE t.slug = ? AND c.level <= ? ORDER BY c.position",
        (slug, depth),
    ).fetchall()
    nodes = [_ref(r) for r in rows]
    for n in nodes:
        n["external"] = False
    pks = [r["pk"] for r in rows]
    edges: List[Tuple[str, str]] = []
    external_slugs: List[str] = []
    if pks:
        own = {r["slug"] for r in rows}
        for a, b in conn.execute(
            """SELECT c1.slug, c2.slug FROM graph.prerequisites p
               JOIN graph.concepts c1 ON c1.pk = p.concept_pk
               JOIN graph.concepts c2 ON c2.pk = p.prereq_pk
               WHERE p.concept_pk = ANY(?) ORDER BY c1.position, c2.position""",
            (pks,),
        ):
            edges.append((a, b))
            if b not in own and b not in external_slugs:
                external_slugs.append(b)
    ext = lessons_by_slug(conn, external_slugs)
    for s in external_slugs:
        if s in ext:
            node = ext[s]
            node["external"] = True
            nodes.append(node)
    known = {n["id"] for n in nodes}
    return {
        "tool": tool,
        "depth": depth,
        "nodes": nodes,
        "edges": [{"concept": a, "requires": b} for a, b in edges if a in known and b in known],
    }


# ------------------------------------------------------------------------ search


def _like_prefix(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _tsquery(tokens: List[str]) -> str:
    return " & ".join([f"'{t}'" for t in tokens[:-1]] + [f"'{tokens[-1]}':*"])


def search_tools(conn: Conn, query: str, limit: int) -> List[dict]:
    """Exact name or alias first, then names starting with the query, then any
    word match; ties in catalog order."""
    q = normalize(query)
    if not q:
        return []
    ranked: Dict[int, Tuple[int, int]] = {}

    def offer(pk: int, position: int, bucket: int) -> None:
        if pk not in ranked or (bucket, position) < ranked[pk]:
            ranked[pk] = (bucket, position)

    for pk, position in conn.execute(
        """SELECT t.pk, t.position FROM graph.tools t WHERE t.name_norm = ?
           UNION SELECT t.pk, t.position FROM graph.tool_aliases a JOIN graph.tools t ON t.pk = a.tool_pk
           WHERE a.alias_norm = ?""",
        (q, q),
    ):
        offer(pk, position, 0)
    for pk, position in conn.execute(
        "SELECT pk, position FROM graph.tools WHERE name_norm LIKE ? ORDER BY position LIMIT ?",
        (_like_prefix(q), limit * 2),
    ):
        offer(pk, position, 1)
    tokens = words(q)
    if tokens:
        for pk, position in conn.execute(
            """SELECT pk, position FROM graph.tools
               WHERE to_tsvector('simple', search_doc) @@ to_tsquery('simple', ?) ORDER BY position LIMIT ?""",
            (_tsquery(tokens), limit * 2),
        ):
            offer(pk, position, 2)
    best = sorted(ranked, key=lambda pk: ranked[pk])[:limit]
    if not best:
        return []
    rows = {r["pk"]: r for r in conn.execute(f"SELECT {_TOOL_COLS} FROM graph.tools t WHERE t.pk = ANY(?)", (best,))}
    return [_tool_card(rows[pk]) for pk in best if pk in rows]


def search_lessons(conn: Conn, query: str, limit: int, tool: Optional[str] = None) -> List[dict]:
    """Exact name, then names starting with the query, then lessons whose name
    (or tool name) contains all its words, the last one as a prefix. Ties go
    to the more important tool and the earlier lesson (pk order)."""
    q = normalize(query)
    if not q:
        return []
    tool_filter, tool_params = "", []
    if tool:
        tool_filter = " AND tool_pk = (SELECT pk FROM graph.tools WHERE slug = ?)"
        tool_params = [tool]
    ranked: Dict[int, Tuple[int, int]] = {}

    def offer(pk: int, bucket: int) -> None:
        if pk not in ranked or (bucket, pk) < ranked[pk]:
            ranked[pk] = (bucket, pk)

    for (pk,) in conn.execute(f"SELECT pk FROM graph.concepts WHERE name_norm = ?{tool_filter}", (q, *tool_params)):
        offer(pk, 0)
    tokens = words(q)
    if tokens:
        for (pk,) in conn.execute(
            f"SELECT pk FROM graph.concepts WHERE name_key LIKE ?{tool_filter} ORDER BY pk LIMIT ?",
            (_like_prefix(" ".join(tokens)), *tool_params, limit * 2),
        ):
            offer(pk, 1)
        for (pk,) in conn.execute(
            f"""SELECT pk FROM graph.concepts
                WHERE to_tsvector('simple', search_doc) @@ to_tsquery('simple', ?){tool_filter}
                ORDER BY pk LIMIT ?""",
            (_tsquery(tokens), *tool_params, limit * 2),
        ):
            offer(pk, 2)
    best = sorted(ranked, key=lambda pk: ranked[pk])[:limit]
    if not best:
        return []
    rows = {r["pk"]: r for r in conn.execute(f"SELECT {_REF_COLS} FROM {_REF_FROM} WHERE c.pk = ANY(?)", (best,))}
    return [_ref(rows[pk]) for pk in best if pk in rows]


# ------------------------------------------------------------ study order helpers


def study_order(nodes: List[dict], edges: List[Tuple[str, str]]) -> List[str]:
    """Ids in an order you could study them: each after all its prerequisites
    (among `nodes`); ready ones by level, then tool study position."""
    import heapq

    ids = {n["id"]: n for n in nodes}
    need = {i: set() for i in ids}
    dependents: Dict[str, List[str]] = {i: [] for i in ids}
    for a, b in edges:
        if a in ids and b in ids:
            need[a].add(b)
            dependents[b].append(a)
    left = {i: len(v) for i, v in need.items()}

    def key(i: str):
        n = ids[i]
        return (n.get("level", 9), n.get("position", 0), i)

    heap = [key(i) for i, k in left.items() if k == 0]
    heapq.heapify(heap)
    order: List[str] = []
    while heap:
        i = heapq.heappop(heap)[-1]
        order.append(i)
        for d in dependents[i]:
            left[d] -= 1
            if left[d] == 0:
                heapq.heappush(heap, key(d))
    return order
