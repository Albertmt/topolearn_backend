#!/usr/bin/env python3
"""Load the tool catalog (data/tools/*.json) into Postgres (schema "graph").

Each file describes one tool and the lessons that teach it; the format is in
data/tools/AUTHORING.md, and scripts/validate_content.py checks a file in
detail. The load itself re-checks what it relies on: every prerequisite points
at a known lesson, a lesson never needs a deeper lesson of its own tool, and
there are no prerequisite loops (across tools too).

Everything is written into a new schema first ("graph_build"), indexed, and
then swapped in for "graph" in one transaction: the API keeps serving the old
catalog until the new one is complete, and never sees a half-loaded state.
User data (schema "public") is never touched; it refers to tools and lessons
by their public ids.

Usage:
    python scripts/build_db.py                          # data/tools/*.json -> $DATABASE_URL
    python scripts/build_db.py data/tools/python.json --database-url postgresql://...
"""
import argparse
import heapq
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
from app.glossary import Glossary  # noqa: E402
from app.repository import normalize, search_words, words  # noqa: E402

SCHEMA = (ROOT / "app" / "schema.sql").read_text()
LIVE, BUILD, OLD = "graph", "graph_build", "graph_old"
_LOAD_LOCK_KEY = 7_405_302  # advisory lock: one loader at a time

# Fallback catalog order when a tool is in no collection (data/collections.json decides otherwise):
# the most asked-for tools first; anything else follows by name.
CATALOG_ORDER = [
    "python", "javascript", "typescript", "java", "sql", "react", "nodejs", "go", "rust", "cpp",
    "numpy", "pandas", "fastapi", "express", "git", "docker",
]


class ContentError(SystemExit):
    pass


LOGOS_FILE = ROOT / "data" / "logos.json"


def read_logos(path: Path = LOGOS_FILE) -> Dict[str, str]:
    """data/logos.json -> {tool id: full <svg> string} (see data/LOGOS.md)."""
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8")).get("logos", {})
    out = {}
    for tid, entry in raw.items():
        d = (entry or {}).get("path", "")
        if d and len(d) < 20_000 and '"' not in d and "<" not in d:
            out[tid] = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="{d}"/></svg>'
    return out


def read_tools(paths: List[Path], logos: Optional[Dict[str, str]] = None) -> List[dict]:
    """Read tool files; a logo from data/logos.json is attached as tool.logo_svg."""
    logos = read_logos() if logos is None else logos
    docs = []
    for p in paths:
        doc = json.loads(p.read_text(encoding="utf-8"))
        tid = doc.get("tool", {}).get("id", "")
        if tid in logos and "logo_svg" not in doc["tool"]:
            doc["tool"]["logo_svg"] = logos[tid]
        docs.append(doc)
    return docs


def prepare(docs: List[dict], order: Optional[List[str]] = None) -> Tuple[List[dict], List[dict], List[Tuple[str, str]]]:
    """Check the documents and work out study order.
    Returns (tools, lessons, edges), edges as (lesson, lesson it requires)."""
    tools: Dict[str, dict] = {}
    lessons: Dict[str, dict] = {}
    for doc in docs:
        t = dict(doc["tool"])
        tid = str(t.get("id", "")).strip()
        if not tid or not str(t.get("name", "")).strip():
            raise ContentError(f"a tool without id/name: {t}")
        if tid in tools:
            raise ContentError(f"tool {tid} is defined twice")
        t["id"] = tid
        t["modules"] = [{"id": m["id"], "name": m["name"]} for m in doc.get("modules", [])]
        t["glossary"] = list(doc.get("glossary") or [])
        tools[tid] = t
        for i, c in enumerate(doc.get("concepts", [])):
            cid = str(c.get("id", "")).strip()
            if not cid.startswith(tid + "."):
                raise ContentError(f"lesson id {cid!r} must start with {tid}.")
            if cid in lessons:
                raise ContentError(f"lesson {cid} is defined twice")
            if c.get("level") not in (1, 2, 3, 4):
                raise ContentError(f"lesson {cid}: level must be 1-4")
            if not str(c.get("name", "")).strip():
                raise ContentError(f"lesson {cid} has no name")
            lessons[cid] = {**c, "id": cid, "tool": tid, "file_order": i}

    edges = set()
    for cid, c in lessons.items():
        for r in c.get("requires", []) or []:
            if r == cid:
                continue
            if r not in lessons:
                print(f"  ! {cid}: unknown prerequisite {r} dropped")
                continue
            if lessons[r]["tool"] == c["tool"] and lessons[r]["level"] > c["level"]:
                raise ContentError(
                    f"{cid} (level {c['level']}) requires {r} (level {lessons[r]['level']}): "
                    "a lesson can't need a deeper lesson of its own tool")
            edges.add((cid, r))
    edges = sorted(edges)

    # No loops, across all tools.
    remaining: Dict[str, int] = defaultdict(int)
    dependents: Dict[str, List[str]] = defaultdict(list)
    for a, b in edges:
        remaining[a] += 1
        dependents[b].append(a)
    queue = deque(c for c in lessons if remaining[c] == 0)
    seen = 0
    while queue:
        n = queue.popleft()
        seen += 1
        for u in dependents[n]:
            remaining[u] -= 1
            if remaining[u] == 0:
                queue.append(u)
    if seen != len(lessons):
        stuck = sorted(c for c in lessons if remaining[c] > 0)
        raise ContentError(f"Prerequisite loop among {len(stuck)} lessons, e.g. {stuck[:10]}")

    for t in tools.values():
        for r in t.get("requires", []) or []:
            if r.get("tool") not in tools:
                print(f"  ! tool {t['id']}: requires unknown tool {r.get('tool')} (ignored)")

    # Study order within each tool: a lesson comes after everything it needs
    # from its own tool; among lessons ready at the same time, shallower level
    # first, then the author's order.
    by_tool: Dict[str, List[str]] = defaultdict(list)
    for cid, c in lessons.items():
        by_tool[c["tool"]].append(cid)
    for ids in by_tool.values():
        local = set(ids)
        left = {cid: 0 for cid in ids}
        local_dependents: Dict[str, List[str]] = defaultdict(list)
        for a, b in edges:
            if a in local and b in local:
                left[a] += 1
                local_dependents[b].append(a)
        heap = [(lessons[c]["level"], lessons[c]["file_order"], c) for c, n in left.items() if n == 0]
        heapq.heapify(heap)
        pos = 0
        while heap:
            _, _, c = heapq.heappop(heap)
            lessons[c]["position"] = pos
            pos += 1
            for d in local_dependents[c]:
                left[d] -= 1
                if left[d] == 0:
                    heapq.heappush(heap, (lessons[d]["level"], lessons[d]["file_order"], d))

    def tool_rank(tid: str):
        explicit = tools[tid].get("position")
        if isinstance(explicit, int):
            return (0, explicit, tid)
        if order and tid in order:
            return (1, order.index(tid), tid)
        if tid in CATALOG_ORDER:
            return (2, CATALOG_ORDER.index(tid), tid)
        return (3, 0, tools[tid]["name"].lower())

    ordered_tools = sorted(tools.values(), key=lambda t: tool_rank(t["id"]))
    ordered_lessons = sorted(lessons.values(), key=lambda c: (tool_rank(c["tool"]), c["position"]))
    return ordered_tools, ordered_lessons, edges


def _schema_parts(schema: str):
    sql = SCHEMA.replace("{schema}", schema)
    before, after = sql.split("-- @after-load", 1)
    return before, after.split("\n", 1)[1]


def build(tools: List[dict], lessons: List[dict], edges: List[Tuple[str, str]], url: str,
          collections: Optional[List[dict]] = None) -> None:
    tool_pk = {t["id"]: i for i, t in enumerate(tools, start=1)}
    lesson_pk = {c["id"]: i for i, c in enumerate(lessons, start=1)}
    tool_names = {t["id"]: t["name"] for t in tools}
    prereq_count: Dict[str, int] = defaultdict(int)
    for a, _ in edges:
        prereq_count[a] += 1
    # Per lesson: terms it uses that the learner hasn't been taught yet, with
    # their definitions (tap/hover in the app and on the website).
    by_tool: Dict[str, List[dict]] = defaultdict(list)
    for c in lessons:
        by_tool[c["tool"]].append(c)
    gloss = Glossary(
        [{"tool": t, "glossary": t.get("glossary") or [], "concepts": by_tool[t["id"]]} for t in tools]
    )
    lesson_terms = {c["id"]: gloss.lesson_glossary(c["id"]) for c in lessons}

    with psycopg.connect(url, prepare_threshold=None, connect_timeout=30) as conn:
        # One transaction for the whole load: if anything fails (or the process
        # dies), nothing is left behind and the live catalog is untouched. A
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
                    f"""COPY {BUILD}.tools (pk, slug, name, name_norm, search_doc, category, tagline,
                        description, icon, color, official_docs, version, position, modules, logo_svg) FROM STDIN"""
                ) as cp:
                    for i, t in enumerate(tools):
                        aliases = [str(a) for a in t.get("aliases") or [] if str(a).strip()]
                        doc = search_words(t["name"]) + [w for a in aliases for w in search_words(a)]
                        cp.write_row((
                            tool_pk[t["id"]], t["id"], t["name"], normalize(t["name"]), " ".join(doc),
                            t.get("category", "tool"), t.get("tagline", ""), t.get("description", ""),
                            t.get("icon", ""), t.get("color", "#4F46E5"), t.get("official_docs"),
                            t.get("version"), i, json.dumps(t.get("modules", [])), t.get("logo_svg"),
                        ))
                seen_alias = set()
                with cur.copy(f"COPY {BUILD}.tool_aliases (tool_pk, alias, alias_norm) FROM STDIN") as cp:
                    for t in tools:
                        for a in [t["id"], *(t.get("aliases") or [])]:
                            norm = normalize(str(a))
                            if norm and (norm, t["id"]) not in seen_alias:
                                seen_alias.add((norm, t["id"]))
                                cp.write_row((tool_pk[t["id"]], str(a), norm))
                with cur.copy(f"COPY {BUILD}.tool_requires (tool_pk, requires_pk, level) FROM STDIN") as cp:
                    for t in tools:
                        done = set()
                        for r in t.get("requires", []) or []:
                            if r.get("tool") in tool_pk and r["tool"] not in done and r["tool"] != t["id"]:
                                done.add(r["tool"])
                                cp.write_row((tool_pk[t["id"]], tool_pk[r["tool"]], int(r.get("level", 1))))
                with cur.copy(
                    f"""COPY {BUILD}.concepts (pk, slug, tool_pk, module, level, position, minutes, name,
                        name_norm, name_key, search_doc, summary, why, body, code, code_lang, takeaways,
                        pitfalls, quiz, docs_url, prereq_count, glossary) FROM STDIN"""
                ) as cp:
                    for c in lessons:
                        doc = search_words(c["name"]) + search_words(tool_names[c["tool"]])
                        cp.write_row((
                            lesson_pk[c["id"]], c["id"], tool_pk[c["tool"]], c.get("module", ""), c["level"],
                            c["position"], int(c.get("minutes") or 10), c["name"], normalize(c["name"]),
                            " ".join(words(c["name"])), " ".join(doc), c.get("summary", ""), c.get("why", ""),
                            c.get("body", ""), c.get("code"), c.get("code_lang"),
                            json.dumps(c.get("takeaways") or []), json.dumps(c.get("pitfalls") or []),
                            json.dumps(c.get("quiz") or []), c.get("docs"), prereq_count[c["id"]],
                            json.dumps(lesson_terms[c["id"]], ensure_ascii=False),
                        ))
                with cur.copy(f"COPY {BUILD}.prerequisites (concept_pk, prereq_pk) FROM STDIN") as cp:
                    for a, b in edges:
                        cp.write_row((lesson_pk[a], lesson_pk[b]))
                cur.executemany(
                    f"INSERT INTO {BUILD}.meta (key, value) VALUES (%s, %s)",
                    [
                        ("built_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
                        ("tools", str(len(tools))),
                        ("concepts", str(len(lessons))),
                        ("prerequisites", str(len(edges))),
                        ("collections", json.dumps(collections or [], ensure_ascii=False)),
                    ],
                )
            conn.execute(finish)
            for table in ("tools", "tool_aliases", "tool_requires", "concepts", "prerequisites"):
                conn.execute(f"ANALYZE {BUILD}.{table}")

            # Swap at commit: readers see either the old catalog or the new one.
            conn.execute(f"DROP SCHEMA IF EXISTS {OLD} CASCADE")
            exists = conn.execute(
                "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (LIVE,)
            ).fetchone()
            if exists:
                conn.execute(f"ALTER SCHEMA {LIVE} RENAME TO {OLD}")
            conn.execute(f"ALTER SCHEMA {BUILD} RENAME TO {LIVE}")
        # Only after the commit: dropping the old tables waits for queries still
        # reading them, and must not block anyone from reading the new catalog.
        conn.execute(f"DROP SCHEMA IF EXISTS {OLD} CASCADE")
        conn.commit()


def prepare_collections(raw, tool_ids) -> List[dict]:
    """Check data/collections.json: curated groups of tools for browsing
    ("Python for data & ML", "Databases & data"...). Unknown tool ids are dropped
    with a warning, so a collection can list tools that aren't written yet."""
    out = []
    seen = set()
    for c in (raw or {}).get("collections", []):
        cid = str(c.get("id", "")).strip()
        if not cid or cid in seen:
            raise ContentError(f"collection ids must be unique and non-empty: {cid!r}")
        seen.add(cid)
        tools = []
        for t in c.get("tools", []):
            if t in tool_ids:
                if t not in tools:
                    tools.append(t)
            else:
                print(f"warning: collection {cid}: unknown tool {t!r} skipped", file=sys.stderr)
        out.append({"id": cid, "name": c.get("name", cid), "description": c.get("description", ""),
                    "icon": c.get("icon", ""), "tools": tools})
    return out


def collection_order(collections: List[dict]) -> List[str]:
    """Tools in the order they first appear in the collections (the catalog order)."""
    return list(dict.fromkeys(t for c in collections for t in c["tools"]))


def load_into(url: str, docs, collections=None) -> Tuple[int, int]:
    """Load in-memory tool documents (one or a list of {"tool", "modules", "concepts"})
    and optional collections ({"collections": [...]}).
    Returns (lessons, prerequisites) loaded. Used by the tests."""
    if isinstance(docs, dict):
        docs = [docs]
    docs = list(docs)
    ids = {d["tool"]["id"] for d in docs}
    cols = prepare_collections(collections, ids) if collections else []
    tools, lessons, edges = prepare(docs, order=collection_order(cols))
    build(tools, lessons, edges, url, cols)
    return len(lessons), len(edges)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("sources", nargs="*", type=Path)
    p.add_argument("--database-url", default=None,
                   help="Postgres connection string (default: $DATABASE_URL, e.g. from .env)")
    args = p.parse_args()

    url = args.database_url or config.DATABASE_URL
    if not url:
        raise SystemExit("No database: pass --database-url or set DATABASE_URL (e.g. in .env).")
    sources = args.sources or sorted((ROOT / "data" / "tools").glob("*.json"))
    if not sources:
        raise SystemExit("No tool files found in data/tools.")

    t0 = time.time()
    docs = read_tools(sources)
    col_file = ROOT / "data" / "collections.json"
    raw = json.loads(col_file.read_text(encoding="utf-8")) if col_file.exists() else None
    cols = prepare_collections(raw, {d["tool"]["id"] for d in docs})
    tools, lessons, edges = prepare(docs, order=collection_order(cols))
    build(tools, lessons, edges, url, cols)
    print(f"Loaded {len(tools)} tools, {len(lessons)} lessons and {len(edges)} prerequisites "
          f"in {time.time() - t0:.1f}s ({len(cols)} collections)")


if __name__ == "__main__":
    main()
