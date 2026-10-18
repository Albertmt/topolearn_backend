"""The catalog: tools, their lesson maps, single lessons, search, and the
public share page. Everything here works signed out; with `user_id` (which
must be the signed-in caller) the answers carry that learner's progress."""
import html
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import HTMLResponse

from . import config, learning, repository, social
from .db import get_conn
from .userdb import reader, session

router = APIRouter(prefix="/api/v1", tags=["catalog"])
pages = APIRouter(tags=["share"])

CATEGORIES = ["language", "library", "framework", "runtime", "tool", "database", "cloud"]
# Catalog data only changes when it's reloaded, so let browsers/CDNs cache it briefly.
CACHE = "public, max-age=300, stale-while-revalidate=3600"
PRIVATE = "private, no-store"


def _learner_counts(tool_ids) -> dict:
    with reader() as conn:
        return {r[0]: r[1] for r in conn.execute(
            "SELECT tool_id, COUNT(*) FROM user_tools WHERE tool_id = ANY(?) GROUP BY tool_id", (list(tool_ids),)
        )}


@router.get("/categories")
def categories():
    return CATEGORIES


@router.get("/tools")
def list_tools(response: Response, category: Optional[str] = Query(None)):
    """Every tool, in catalog order, with lesson counts and study time per depth level."""
    tools = repository.list_tools(get_conn(), category)
    counts = _learner_counts([t["id"] for t in tools])
    for t in tools:
        t["learners"] = counts.get(t["id"], 0)
    response.headers["Cache-Control"] = "public, max-age=60"
    return tools


@router.get("/collections")
def list_collections(response: Response):
    """The catalog grouped for browsing ("Programming languages", "Python for data & ML"...),
    each with its tools as cards, in curated order. A tool can be in several
    collections; tools in none are listed last under "More tools"."""
    tools = list_tools(response, None)
    by_id = {t["id"]: t for t in tools}
    out, listed = [], set()
    for c in repository.collections(get_conn()):
        cards = [by_id[t] for t in c.get("tools", []) if t in by_id]
        listed.update(t["id"] for t in cards)
        if cards:
            out.append({"id": c["id"], "name": c["name"], "description": c.get("description", ""),
                        "icon": c.get("icon", ""), "tools": cards})
    rest = [t for t in tools if t["id"] not in listed]
    if rest:
        out.append({"id": "more", "name": "More tools" if out else "All tools", "description": "",
                    "icon": "", "tools": rest})
    return out


@router.get("/tools/{tool_id}")
def get_tool(tool_id: str, response: Response, user_id: Optional[str] = None):
    tool = repository.get_tool(get_conn(), tool_id)
    if tool is None:
        raise HTTPException(404, f"Unknown tool '{tool_id}'")
    tool["learners"] = _learner_counts([tool_id]).get(tool_id, 0)
    if user_id:
        mine = learning.enrollments(user_id).get(tool_id)
        tool["enrolled"] = mine is not None
        depth = mine["depth"] if mine else learning.default_depth(user_id)
        tool["progress"] = learning.tool_summaries(user_id, {tool_id: depth})[tool_id]
        req = {r["id"]: r["level"] for r in tool["requires"]}
        if req:
            prereq_progress = learning.tool_summaries(user_id, req, limit_next=0)
            for r in tool["requires"]:
                s = prereq_progress[r["id"]]
                r["progress"] = {k: s[k] for k in ("percent", "done", "total", "complete")}
        response.headers["Cache-Control"] = PRIVATE
    else:
        response.headers["Cache-Control"] = CACHE
    return tool


@router.get("/tools/{tool_id}/map")
def tool_map(tool_id: str, response: Response, depth: Optional[int] = Query(None, ge=1, le=4),
             user_id: Optional[str] = None):
    """The dependency tree of a tool down to a depth level: lessons (with the
    other tools' lessons they build on, marked `external`) and the edges
    between them (`concept` requires `requires`). Foundations are the lessons
    nothing is required for. With `user_id`: each node carries the learner's
    progress, and `progress` sums it up with the lessons to do next.
    Without `depth`: the learner's chosen depth, else a sensible default."""
    conn = get_conn()
    mine = learning.enrollments(user_id).get(tool_id) if user_id else None
    d = depth or (mine["depth"] if mine else learning.default_depth(user_id))
    m = repository.tool_map(conn, tool_id, d)
    if m is None:
        raise HTTPException(404, f"Unknown tool '{tool_id}'")
    ids = [n["id"] for n in m["nodes"]]
    m["order"] = repository.study_order(
        [n for n in m["nodes"] if not n["external"]], [(e["concept"], e["requires"]) for e in m["edges"]]
    )
    if not user_id:
        response.headers["Cache-Control"] = CACHE
        m["enrolled"] = False
        return m
    prog = learning.progress_for(user_id, ids)
    with reader() as c:
        saved = {r[0] for r in c.execute(
            "SELECT concept_id FROM saved_lessons WHERE user_id = ? AND concept_id = ANY(?)", (user_id, ids))}
    for n in m["nodes"]:
        p = prog.get(n["id"]) or {}
        n["understanding"] = p.get("understanding")
        n["status"] = p.get("status")
        n["saved"] = n["id"] in saved
    own = [n for n in m["nodes"] if not n["external"]]
    requires = {}
    for e in m["edges"]:
        requires.setdefault(e["concept"], []).append(e["requires"])
    ext = {n["id"]: n for n in m["nodes"] if n["external"]}
    m["progress"] = learning.summarize(tool_id, d, own, requires, prog, ext)
    m["enrolled"] = mine is not None
    if mine:
        with session() as c:
            c.execute("UPDATE user_tools SET last_opened_at = ? WHERE user_id = ? AND tool_id = ?",
                      (time.time(), user_id, tool_id))
    response.headers["Cache-Control"] = PRIVATE
    return m


@router.get("/lessons/{lesson_id}")
def get_lesson(lesson_id: str, response: Response, user_id: Optional[str] = None):
    """A whole lesson: the tutorial, code, takeaways, quiz, the lessons it
    builds on and unlocks, reactions and comment count; with `user_id` also the
    learner's progress and saved state."""
    conn = get_conn()
    lesson = repository.get_lesson(conn, lesson_id)
    if lesson is None:
        raise HTTPException(404, f"Unknown lesson '{lesson_id}'")
    with reader() as c:
        lesson["social"] = social.lesson_social(c, lesson_id, user_id)
    if user_id:
        ids = [lesson_id] + [r["id"] for r in lesson["requires"]] + [u["id"] for u in lesson["unlocks"]]
        prog = learning.progress_for(user_id, ids)
        p = prog.get(lesson_id) or {}
        lesson["understanding"] = p.get("understanding")
        lesson["status"] = p.get("status")
        for group in ("requires", "unlocks"):
            for r in lesson[group]:
                q = prog.get(r["id"]) or {}
                r["understanding"] = q.get("understanding")
                r["status"] = q.get("status")
        lesson["ready"] = all(learning.done(prog.get(r["id"])) for r in lesson["requires"])
        response.headers["Cache-Control"] = PRIVATE
    else:
        response.headers["Cache-Control"] = "public, max-age=60"
    return lesson


@router.get("/search")
def search(response: Response, q: str = Query(..., min_length=1, max_length=100),
           limit: int = Query(8, ge=1, le=config.SEARCH_LIMIT), tool: Optional[str] = Query(None)):
    """Tools first (exact name or alias, then prefix, then any word), then lessons.
    With `tool`: only that tool's lessons."""
    conn = get_conn()
    response.headers["Cache-Control"] = CACHE
    return {
        "query": q,
        "tools": [] if tool else repository.search_tools(conn, q, min(limit, 6)),
        "lessons": repository.search_lessons(conn, q, limit, tool),
    }


# ------------------------------------------------------------- share pages


@pages.get("/t/{tool_id}", response_class=HTMLResponse, include_in_schema=False)
def share_page(tool_id: str, depth: int = Query(2, ge=1, le=4)):
    """A public, read-only page with a tool's curriculum down to a depth, in
    study order, so a teacher or friend can send "how to learn X" as a link."""
    m = repository.tool_map(get_conn(), tool_id, depth)
    if m is None:
        raise HTTPException(404, f"Unknown tool '{tool_id}'")
    esc = html.escape
    tool = m["tool"]
    own = [n for n in m["nodes"] if not n["external"]]
    order = repository.study_order(own, [(e["concept"], e["requires"]) for e in m["edges"]])
    by_id = {n["id"]: n for n in own}
    hours = sum(n["minutes"] for n in own) / 60
    items = "\n".join(
        f"""<li><strong>{esc(by_id[i]['name'])}</strong>
              <span class="meta">{repository.LEVEL_NAMES[by_id[i]['level']]} · {by_id[i]['minutes']} min</span>
              <p>{esc(by_id[i]['summary'] or '')}</p></li>"""
        for i in order
    )
    color = esc(tool["color"])
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Learn {esc(tool['name'])} · Topolearn</title>
<meta name="description" content="{len(own)} lessons, in order, to learn {esc(tool['name'])} to {repository.LEVEL_NAMES[m['depth']]} level.">
<style>
 :root {{ --fg:#1b1d2a; --muted:#5b6070; --accent:{color}; --bg:#f7f8fc; --card:#fff; }}
 @media (prefers-color-scheme: dark) {{ :root {{ --fg:#e8e9f0; --muted:#a2a6b5; --bg:#12131a; --card:#1c1e28; }} }}
 body {{ font: 16px/1.5 system-ui, -apple-system, Segoe UI, sans-serif; color: var(--fg); background: var(--bg);
        margin: 0; padding: 24px 16px; }}
 main {{ max-width: 720px; margin: 0 auto; }}
 h1 {{ margin: 0 0 4px; font-size: 1.8rem; }}
 .badge {{ display:inline-grid; place-items:center; width:44px; height:44px; border-radius:12px; background:var(--accent);
          color:#fff; font-weight:800; margin-bottom:12px; }}
 .sub {{ color: var(--muted); margin-bottom: 24px; }}
 ol {{ padding-left: 1.4rem; }}
 li {{ background: var(--card); border-radius: 12px; padding: 12px 16px; margin: 0 0 10px; }}
 li p {{ margin: 4px 0 0; color: var(--muted); font-size: .95rem; }}
 .meta {{ color: var(--muted); font-size: .85rem; margin-left: 8px; }}
 footer {{ color: var(--muted); font-size: .85rem; margin-top: 24px; }}
 a {{ color: inherit; }}
</style></head><body><main>
<div class="badge">{esc(tool['icon'])}</div>
<h1>Learn {esc(tool['name'])}</h1>
<div class="sub">{len(own)} lessons to {repository.LEVEL_NAMES[m['depth']]} level, foundations first · about {hours:.0f} hours</div>
<ol>
{items}
</ol>
<footer>Made with <a href="https://topolearnapp.com">Topolearn</a> — learn any tool, one step at a time.</footer>
</main></body></html>"""
    return HTMLResponse(page, headers={"Cache-Control": "public, max-age=3600"})
