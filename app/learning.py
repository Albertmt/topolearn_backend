"""Per-user learning state, learning paths, goals, stats, community sources
and problem reports.

Progress (per user × concept):
  understanding 0..100 (None = not rated), status None | need_to_learn | hidden.
  Hidden concepts are pruned from that user's graphs and paths.

Auth is still the placeholder login, so the acting user is passed as `user_id`.
When real sign-in arrives, take it from the session token instead.
"""
import html
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Literal, Optional, Set
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator

from . import repository, userdb
from .db import Conn, Row, get_conn
from .userdb import reader, session, user_exists

router = APIRouter(prefix="/api/v1", tags=["learning"])
pages = APIRouter(tags=["share"])

Status = Literal["need_to_learn", "hidden"]
SOURCE_KINDS = ["video", "course", "article", "book", "docs", "interactive", "paper", "other"]
REPORT_KINDS = [
    "wrong_prerequisite",   # concept → related_concept link shouldn't exist
    "missing_prerequisite", # concept should require related_concept
    "wrong_description",
    "wrong_example",
    "duplicate",            # concept is the same as related_concept
    "other",
]
MAX_GOALS = 5
HELPED_WEIGHT = 2  # a "this helped me" counts as two upvotes when ranking


# --------------------------------------------------------------- helpers


def _require_user(conn: Conn, user_id: str) -> None:
    if not user_exists(conn, user_id):
        raise HTTPException(404, "User not found")


def _require_concept(concept_id: str) -> dict:
    c = repository.get_concept(get_conn(), concept_id)
    if c is None:
        raise HTTPException(404, f"Unknown concept '{concept_id}'")
    return c


def _ref(concept_id: str) -> Optional[dict]:
    c = repository.get_concept(get_conn(), concept_id)
    return {"id": c["id"], "name": c["name"]} if c else None


def _refs(concept_ids: Iterable[str]) -> Dict[str, dict]:
    """{id: {"id", "name"}} for many concepts in one query (unknown ids left out)."""
    found = repository.concepts_by_slug(get_conn(), concept_ids)
    return {cid: {"id": c["id"], "name": c["name"]} for cid, c in found.items()}


def hidden_concepts(user_id: str) -> Set[str]:
    with reader() as conn:
        rows = conn.execute(
            "SELECT concept_id FROM concept_progress WHERE user_id = ? AND status = 'hidden'",
            (user_id,),
        ).fetchall()
    return {r[0] for r in rows}


def progress_for(user_id: str, concept_ids: Iterable[str]) -> Dict[str, dict]:
    ids = list(concept_ids)
    out: Dict[str, dict] = {}
    if not ids:
        return out
    with reader() as conn:
        for r in conn.execute(
            """SELECT concept_id, understanding, status FROM concept_progress
               WHERE user_id = ? AND concept_id = ANY(?)""",
            (user_id, ids),
        ):
            out[r["concept_id"]] = {"understanding": r["understanding"], "status": r["status"]}
    return out


def _understood(p: Optional[dict]) -> bool:
    return bool(p) and (p.get("understanding") or 0) >= 100


# --------------------------------------------------------------- progress


class ConceptRef(BaseModel):
    id: str
    name: str


class Progress(BaseModel):
    concept_id: str
    name: Optional[str] = None
    understanding: Optional[int] = None
    status: Optional[Status] = None
    newly_ready: List[ConceptRef] = Field(
        [], description="Concepts that just became learnable because this one is now understood"
    )


class ProgressUpdate(BaseModel):
    """Send only what changes. `null` clears a field."""

    understanding: Optional[int] = Field(None, ge=0, le=100)
    status: Optional[Status] = None


def _progress_row(conn, user_id: str, concept_id: str) -> Optional[Row]:
    return conn.execute(
        "SELECT understanding, status FROM concept_progress WHERE user_id = ? AND concept_id = ?",
        (user_id, concept_id),
    ).fetchone()


def _newly_ready(user_id: str, concept_id: str) -> List[dict]:
    """Direct dependents of `concept_id` whose prerequisites are now all understood."""
    concepts = get_conn()
    dependents = repository.direct_dependents(concepts, concept_id)
    if not dependents:
        return []
    needed: Dict[str, List[str]] = repository.direct_prereqs_many(concepts, [d["id"] for d in dependents])
    all_ids = {concept_id} | set(needed) | {p for ps in needed.values() for p in ps}
    prog = progress_for(user_id, all_ids)
    ready = []
    for d in dependents:
        mine = prog.get(d["id"])
        if _understood(mine) or (mine or {}).get("status") == "hidden":
            continue
        if all(_understood(prog.get(p)) for p in needed[d["id"]]):
            ready.append({"id": d["id"], "name": d["name"]})
    return ready[:5]


@router.get("/users/{user_id}/progress", response_model=List[Progress])
def list_progress(
    user_id: str,
    status: Optional[Status] = Query(None, description="Only rows with this status"),
):
    with reader() as conn:
        _require_user(conn, user_id)
        sql = "SELECT concept_id, understanding, status FROM concept_progress WHERE user_id = ?"
        args: list = [user_id]
        if status:
            sql += " AND status = ?"
            args.append(status)
        rows = conn.execute(sql + " ORDER BY updated_at DESC", args).fetchall()
    found = repository.concepts_by_slug(get_conn(), [r["concept_id"] for r in rows])
    out = []
    for r in rows:
        c = found.get(r["concept_id"])
        out.append({
            "concept_id": r["concept_id"],
            "name": c["name"] if c else None,
            "understanding": r["understanding"],
            "status": r["status"],
        })
    return out


@router.put("/users/{user_id}/progress/{concept_id}", response_model=Progress)
def set_progress(user_id: str, concept_id: str, body: ProgressUpdate):
    concept = _require_concept(concept_id)
    changes = body.model_dump(exclude_unset=True)
    now = time.time()
    with session() as conn:
        _require_user(conn, user_id)
        row = _progress_row(conn, user_id, concept_id)
        before = row["understanding"] if row else None
        understanding = before
        status = row["status"] if row else None
        if "understanding" in changes:
            understanding = changes["understanding"]
        if "status" in changes:
            status = changes["status"]
        if understanding is None and status is None:
            conn.execute(
                "DELETE FROM concept_progress WHERE user_id = ? AND concept_id = ?",
                (user_id, concept_id),
            )
        else:
            conn.execute(
                """INSERT INTO concept_progress (user_id, concept_id, understanding, status, updated_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT (user_id, concept_id) DO UPDATE SET
                     understanding = excluded.understanding,
                     status = excluded.status,
                     updated_at = excluded.updated_at""",
                (user_id, concept_id, understanding, status, now),
            )
        conn.execute(
            "INSERT INTO progress_events (user_id, concept_id, understanding, status, at) VALUES (?,?,?,?,?)",
            (user_id, concept_id, understanding, status, now),
        )
    just_mastered = (understanding or 0) >= 100 and (before or 0) < 100
    return {
        "concept_id": concept_id,
        "name": concept["name"],
        "understanding": understanding,
        "status": status,
        "newly_ready": _newly_ready(user_id, concept_id) if just_mastered else [],
    }


@router.delete("/users/{user_id}/progress/{concept_id}", status_code=204)
def clear_progress(user_id: str, concept_id: str):
    with session() as conn:
        _require_user(conn, user_id)
        conn.execute(
            "DELETE FROM concept_progress WHERE user_id = ? AND concept_id = ?",
            (user_id, concept_id),
        )
        conn.execute(
            "INSERT INTO progress_events (user_id, concept_id, understanding, status, at) VALUES (?,?,?,?,?)",
            (user_id, concept_id, None, None, time.time()),
        )
    return Response(status_code=204)


# ------------------------------------------------------------ learning path


class PathStep(BaseModel):
    id: str
    name: str
    type: Optional[str] = None
    domain: Optional[str] = None
    description: Optional[str] = None
    difficulty: Optional[float] = None
    requires: List[str] = []
    minutes: int
    understanding: Optional[int] = None
    status: Optional[str] = None
    done: bool = False
    ready: bool = Field(False, description="Not done yet, and every prerequisite is done")


class CoveringSource(BaseModel):
    id: str
    title: str
    url: str
    kind: str
    covers: int = Field(..., description="How many of the remaining steps it teaches")
    score: int


class LearningPath(BaseModel):
    root: str
    name: str
    steps: List[PathStep]
    total: int
    done: int
    remaining: int
    remaining_minutes: int
    truncated: bool
    covering_sources: List[CoveringSource] = []


def build_path(concept_id: str, user_id: Optional[str]) -> dict:
    skip = hidden_concepts(user_id) if user_id else set()
    p = repository.learning_path(get_conn(), concept_id, skip=skip)
    if p is None:
        raise HTTPException(404, f"Unknown concept '{concept_id}'")
    prog = progress_for(user_id, [s["id"] for s in p["steps"]]) if user_id else {}
    done_ids = {sid for sid, v in prog.items() if _understood(v)}
    steps, remaining_minutes = [], 0
    for s in p["steps"]:
        mine = prog.get(s["id"]) or {}
        s["understanding"] = mine.get("understanding")
        s["status"] = mine.get("status")
        s["done"] = s["id"] in done_ids
        s["ready"] = not s["done"] and all(r in done_ids for r in s["requires"])
        if not s["done"]:
            remaining_minutes += int(round(s["minutes"] * (1 - (s["understanding"] or 0) / 100)))
        steps.append(s)
    remaining_ids = [s["id"] for s in steps if not s["done"]]
    return {
        "root": p["root"],
        "name": steps[-1]["name"] if steps else concept_id,
        "steps": steps,
        "total": len(steps),
        "done": len(steps) - len(remaining_ids),
        "remaining": len(remaining_ids),
        "remaining_minutes": remaining_minutes,
        "truncated": p["truncated"],
        "covering_sources": _covering_sources(remaining_ids),
    }


def _covering_sources(concept_ids: List[str], limit: int = 5) -> List[dict]:
    """Sources that teach the most of these concepts (at least two)."""
    if len(concept_ids) < 2:
        return []
    with reader() as conn:
        rows = conn.execute(
            """SELECT s.id, s.title, s.url, s.kind, COUNT(DISTINCT sc.concept_id) AS covers,
                      (SELECT COALESCE(SUM(v.value), 0) FROM source_votes v WHERE v.source_id = s.id) AS score
               FROM source_concepts sc JOIN sources s ON s.id = sc.source_id
               WHERE sc.concept_id = ANY(?)
               GROUP BY s.id HAVING COUNT(DISTINCT sc.concept_id) >= 2
               ORDER BY covers DESC, score DESC, s.id LIMIT ?""",
            (list(concept_ids), limit),
        ).fetchall()
    return [dict(r) for r in rows]


@router.get("/concepts/{concept_id}/path", response_model=LearningPath)
def learning_path(concept_id: str, user_id: Optional[str] = None):
    """Everything to learn before `concept_id`, in study order (foundations
    first, the concept itself last). With `user_id`: hidden concepts pruned,
    each step marked done/ready, remaining time estimated."""
    return build_path(concept_id, user_id)


# --------------------------------------------------------------------- goals


class Goal(BaseModel):
    concept: ConceptRef
    total: int
    done: int
    remaining_minutes: int
    next: List[ConceptRef] = Field([], description="Up to 3 steps that are ready to learn now")
    created_at: float


def _goal(user_id: str, concept_id: str, created_at: float) -> Optional[dict]:
    ref = _ref(concept_id)
    if ref is None:
        return None  # concept disappeared in a data rebuild
    p = build_path(concept_id, user_id)
    return {
        "concept": ref,
        "total": p["total"],
        "done": p["done"],
        "remaining_minutes": p["remaining_minutes"],
        "next": [{"id": s["id"], "name": s["name"]} for s in p["steps"] if s["ready"]][:3],
        "created_at": created_at,
    }


@router.get("/users/{user_id}/goals", response_model=List[Goal])
def list_goals(user_id: str):
    with reader() as conn:
        _require_user(conn, user_id)
        rows = conn.execute(
            "SELECT concept_id, created_at FROM user_goals WHERE user_id = ? ORDER BY created_at",
            (user_id,),
        ).fetchall()
    return [g for g in (_goal(user_id, r["concept_id"], r["created_at"]) for r in rows) if g]


@router.put("/users/{user_id}/goals/{concept_id}", response_model=Goal)
def add_goal(user_id: str, concept_id: str):
    _require_concept(concept_id)
    with session() as conn:
        # Lock this learner's row: concurrent goal edits for the same person run
        # one after another, so the limit holds and nothing is inserted twice.
        if conn.execute("SELECT 1 FROM users WHERE id = ? FOR UPDATE", (user_id,)).fetchone() is None:
            raise HTTPException(404, "User not found")
        existing = conn.execute(
            "SELECT created_at FROM user_goals WHERE user_id = ? AND concept_id = ?", (user_id, concept_id)
        ).fetchone()
        if existing is None:
            count = conn.execute("SELECT COUNT(*) FROM user_goals WHERE user_id = ?", (user_id,)).fetchone()[0]
            if count >= MAX_GOALS:
                raise HTTPException(409, f"You can pin up to {MAX_GOALS} goals. Remove one first.")
            created = time.time()
            conn.execute(
                """INSERT INTO user_goals (user_id, concept_id, created_at) VALUES (?,?,?)
                   ON CONFLICT (user_id, concept_id) DO NOTHING""",
                (user_id, concept_id, created),
            )
        else:
            created = existing["created_at"]
    return _goal(user_id, concept_id, created)


@router.delete("/users/{user_id}/goals/{concept_id}", status_code=204)
def remove_goal(user_id: str, concept_id: str):
    with session() as conn:
        _require_user(conn, user_id)
        conn.execute("DELETE FROM user_goals WHERE user_id = ? AND concept_id = ?", (user_id, concept_id))
    return Response(status_code=204)


# --------------------------------------------------------------------- stats


class WeekCount(BaseModel):
    week_start: str  # ISO date (Monday)
    mastered: int


class Stats(BaseModel):
    understood: int
    partly: int
    need_to_learn: int
    hidden: int
    streak_days: int = Field(..., description="Consecutive days with any progress, ending today or yesterday")
    best_streak: int
    mastered_this_week: int
    weekly: List[WeekCount]


@router.get("/users/{user_id}/stats", response_model=Stats)
def stats(user_id: str, tz_offset_minutes: int = Query(0, ge=-14 * 60, le=14 * 60)):
    """`tz_offset_minutes`: the device's UTC offset, so "today" matches the learner's day."""
    tz = timezone(timedelta(minutes=tz_offset_minutes))
    with reader() as conn:
        _require_user(conn, user_id)
        counts = conn.execute(
            """SELECT
                 SUM(CASE WHEN understanding >= 100 THEN 1 ELSE 0 END),
                 SUM(CASE WHEN understanding > 0 AND understanding < 100 THEN 1 ELSE 0 END),
                 SUM(CASE WHEN status = 'need_to_learn' THEN 1 ELSE 0 END),
                 SUM(CASE WHEN status = 'hidden' THEN 1 ELSE 0 END)
               FROM concept_progress WHERE user_id = ?""",
            (user_id,),
        ).fetchone()
        events = conn.execute(
            "SELECT concept_id, understanding, at FROM progress_events WHERE user_id = ? ORDER BY at",
            (user_id,),
        ).fetchall()

    today = datetime.now(tz).date()
    days = sorted({datetime.fromtimestamp(e["at"], tz).date() for e in events})

    # streaks
    best = run = 0
    prev = None
    for d in days:
        run = run + 1 if prev is not None and (d - prev).days == 1 else 1
        best = max(best, run)
        prev = d
    current = 0
    if days and (today - days[-1]).days <= 1:
        current = 1
        for a, b in zip(reversed(days[:-1]), reversed(days[1:])):
            if (b - a).days == 1:
                current += 1
            else:
                break

    # first time each concept reached 100%, bucketed by week
    first_mastered: Dict[str, datetime] = {}
    for e in events:
        if (e["understanding"] or 0) >= 100 and e["concept_id"] not in first_mastered:
            first_mastered[e["concept_id"]] = datetime.fromtimestamp(e["at"], tz)
    this_monday = today - timedelta(days=today.weekday())
    weeks = [this_monday - timedelta(weeks=i) for i in range(7, -1, -1)]
    per_week = {w: 0 for w in weeks}
    for when in first_mastered.values():
        d = when.date()
        w = d - timedelta(days=d.weekday())
        if w in per_week:
            per_week[w] += 1

    return {
        "understood": counts[0] or 0,
        "partly": counts[1] or 0,
        "need_to_learn": counts[2] or 0,
        "hidden": counts[3] or 0,
        "streak_days": current,
        "best_streak": best,
        "mastered_this_week": per_week[this_monday],
        "weekly": [{"week_start": w.isoformat(), "mastered": per_week[w]} for w in weeks],
    }


# ---------------------------------------------------------------------- feed


class FeedItem(BaseModel):
    id: str
    name: str
    understanding: Optional[int] = None
    status: Optional[str] = None
    reason: Optional[str] = Field(None, description="Why it's suggested, e.g. 'For your goal: Transformer'")
    count: Optional[int] = Field(None, description="Popular: learners active on it this week")


class Feed(BaseModel):
    continue_learning: List[FeedItem]
    ready_next: List[FeedItem]
    recently_mastered: List[FeedItem]
    popular: List[FeedItem]


@router.get("/users/{user_id}/feed", response_model=Feed)
def feed(user_id: str):
    """Home-screen feed: pick up where you left off, what's ready next (from
    goals, then interests), recently mastered, and what others study."""
    import json as _json

    week_ago = time.time() - 7 * 86400
    with reader() as conn:
        _require_user(conn, user_id)
        in_progress = conn.execute(
            """SELECT concept_id, understanding, status FROM concept_progress
               WHERE user_id = ? AND COALESCE(status, '') != 'hidden'
                 AND (COALESCE(understanding, 0) < 100 OR status = 'need_to_learn')
               ORDER BY updated_at DESC LIMIT 8""",
            (user_id,),
        ).fetchall()
        mastered = conn.execute(
            """SELECT concept_id, MAX(at) AS at FROM progress_events
               WHERE user_id = ? AND understanding >= 100
               GROUP BY concept_id ORDER BY at DESC LIMIT 6""",
            (user_id,),
        ).fetchall()
        goals = [r[0] for r in conn.execute(
            "SELECT concept_id FROM user_goals WHERE user_id = ? ORDER BY created_at", (user_id,))]
        interests = _json.loads(conn.execute(
            "SELECT interests FROM users WHERE id = ?", (user_id,)).fetchone()[0] or "[]")
        popular_rows = conn.execute(
            """SELECT concept_id, COUNT(DISTINCT user_id) AS n FROM progress_events
               WHERE at >= ? AND user_id != ? GROUP BY concept_id ORDER BY n DESC LIMIT 20""",
            (week_ago, user_id),
        ).fetchall()

    refs = _refs([r["concept_id"] for r in [*in_progress, *mastered, *popular_rows]])

    def item(cid: str, **extra) -> Optional[dict]:
        ref = refs.get(cid)
        return {**ref, **extra} if ref else None

    still_current = {r["concept_id"] for r in in_progress}
    continue_learning = [x for x in (item(r["concept_id"], understanding=r["understanding"], status=r["status"])
                                     for r in in_progress[:5]) if x]
    recently_mastered = [x for x in (item(r["concept_id"], understanding=100) for r in mastered[:3]
                                     if r["concept_id"] not in still_current) if x]

    # Ready next: walk goal paths first, then interest paths, until we have 6.
    hidden = hidden_concepts(user_id)
    known_targets = set(repository.concepts_by_slug(get_conn(), [*goals, *interests[:3]]))
    ready, seen = [], set(still_current)
    for source, label in [(goals, "For your goal"), (interests[:3], "In")]:
        for target in source:
            if len(ready) >= 6 or target not in known_targets:
                continue
            try:
                p = build_path(target, user_id)
            except HTTPException:
                continue
            for s in p["steps"]:
                if s["ready"] and s["id"] not in seen and s["id"] not in hidden:
                    seen.add(s["id"])
                    ready.append({"id": s["id"], "name": s["name"], "understanding": s["understanding"],
                                  "status": s["status"], "reason": f"{label}: {p['name']}"})
                    if len(ready) >= 6:
                        break

    mine = progress_for(user_id, [r["concept_id"] for r in popular_rows])
    popular = []
    for r in popular_rows:
        if _understood(mine.get(r["concept_id"])) or r["concept_id"] in hidden:
            continue
        x = item(r["concept_id"], count=r["n"])
        if x:
            popular.append(x)
        if len(popular) >= 5:
            break

    return {
        "continue_learning": continue_learning,
        "ready_next": ready,
        "recently_mastered": recently_mastered,
        "popular": popular,
    }


# ---------------------------------------------------------------- sources


class Source(BaseModel):
    id: str
    concept_id: str
    url: str
    title: str
    kind: str
    score: int
    upvotes: int
    downvotes: int
    my_vote: int = 0
    helped_count: int = 0
    helped_by_me: bool = False
    added_by_me: bool = False
    added_by_name: Optional[str] = None
    covers: List[ConceptRef] = Field([], description="Every concept this source teaches")
    created_at: float


def _clean_url(v: str) -> str:
    v = v.strip()
    if not v.lower().startswith(("http://", "https://")):
        v = "https://" + v
    p = urlparse(v)
    host = p.hostname or ""
    if p.scheme not in ("http", "https") or "." not in host or " " in v:
        raise ValueError("must be a web link, e.g. https://example.com/page")
    return v


class SourceCreate(BaseModel):
    user_id: str
    url: str = Field(..., max_length=2000)
    title: Optional[str] = Field(None, max_length=200)
    kind: str = "other"
    also_covers: List[str] = Field([], max_length=20, description="Other concept ids this teaches")

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        return _clean_url(v)

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in SOURCE_KINDS:
            raise ValueError(f"must be one of {SOURCE_KINDS}")
        return v


class VoteBody(BaseModel):
    user_id: str
    value: Literal[-1, 0, 1]


class HelpedBody(BaseModel):
    user_id: str
    helped: bool


_SOURCE_SELECT = """
SELECT s.id, s.concept_id, s.url, s.title, s.kind, s.added_by, s.created_at, u.name AS added_by_name,
       (SELECT COALESCE(SUM(v.value), 0) FROM source_votes v WHERE v.source_id = s.id) AS score,
       (SELECT COUNT(*) FROM source_votes v WHERE v.source_id = s.id AND v.value = 1)  AS upvotes,
       (SELECT COUNT(*) FROM source_votes v WHERE v.source_id = s.id AND v.value = -1) AS downvotes,
       (SELECT COALESCE(MAX(v.value), 0) FROM source_votes v WHERE v.source_id = s.id AND v.user_id = :me) AS my_vote,
       (SELECT COUNT(*) FROM source_helped h WHERE h.source_id = s.id) AS helped_count,
       (SELECT COUNT(*) FROM source_helped h WHERE h.source_id = s.id AND h.user_id = :me) AS helped_by_me
FROM sources s
LEFT JOIN users u ON u.id = s.added_by
"""


def _source(conn, r: Row, me: Optional[str]) -> dict:
    d = dict(r)
    added_by = d.pop("added_by", None)
    d["added_by_me"] = me is not None and added_by == me
    d["helped_by_me"] = bool(d["helped_by_me"])
    covers = [x[0] for x in conn.execute(
        "SELECT concept_id FROM source_concepts WHERE source_id = ?", (d["id"],))]
    # main concept first, then the rest
    covers.sort(key=lambda c: (c != d["concept_id"], c))
    refs = _refs(covers)
    d["covers"] = [refs[c] for c in covers if c in refs]
    return d


def _get_source(conn, source_id: str, me: Optional[str]) -> dict:
    r = conn.execute(_SOURCE_SELECT + " WHERE s.id = :sid", {"me": me, "sid": source_id}).fetchone()
    if r is None:
        raise HTTPException(404, "Source not found")
    return _source(conn, r, me)


@router.get("/concepts/{concept_id}/sources", response_model=List[Source])
def list_sources(concept_id: str, user_id: Optional[str] = None, kind: Optional[str] = None):
    """Sources that teach this concept (added here or tagged "also covers"),
    best first. Optional `kind` filter (video, course, …)."""
    _require_concept(concept_id)
    with reader() as conn:
        sql = _SOURCE_SELECT + """
            WHERE s.id IN (SELECT source_id FROM source_concepts WHERE concept_id = :cid)"""
        params = {"me": user_id, "cid": concept_id}
        if kind:
            sql += " AND s.kind = :kind"
            params["kind"] = kind
        rows = conn.execute(sql, params).fetchall()
        out = [_source(conn, r, user_id) for r in rows]
    out.sort(key=lambda s: (-(s["score"] + HELPED_WEIGHT * s["helped_count"]), s["created_at"]))
    return out


@router.post("/concepts/{concept_id}/sources", response_model=Source, status_code=201)
def add_source(concept_id: str, body: SourceCreate):
    _require_concept(concept_id)
    also = [c for c in dict.fromkeys(body.also_covers) if c != concept_id]
    known = repository.concepts_by_slug(get_conn(), also)
    unknown = [c for c in also if c not in known]
    if unknown:
        raise HTTPException(422, f"Unknown concept id(s): {', '.join(unknown[:5])}")
    title = (body.title or "").strip() or (urlparse(body.url).hostname or body.url).removeprefix("www.")
    sid = uuid.uuid4().hex
    try:
        with session() as conn:
            _require_user(conn, body.user_id)
            conn.execute(
                """INSERT INTO sources (id, concept_id, url, title, kind, added_by, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (sid, concept_id, body.url, title, body.kind, body.user_id, time.time()),
            )
            conn.executemany("INSERT INTO source_concepts (source_id, concept_id) VALUES (?,?)",
                             [(sid, c) for c in [concept_id, *also]])
            # Adding a source counts as recommending it.
            conn.execute("INSERT INTO source_votes (source_id, user_id, value) VALUES (?,?,1)",
                         (sid, body.user_id))
            return _get_source(conn, sid, body.user_id)
    except userdb.IntegrityError:
        raise HTTPException(409, "This link has already been added for this concept")


@router.put("/sources/{source_id}/vote", response_model=Source)
def vote(source_id: str, body: VoteBody):
    with session() as conn:
        _require_user(conn, body.user_id)
        _get_source(conn, source_id, body.user_id)
        if body.value == 0:
            conn.execute("DELETE FROM source_votes WHERE source_id = ? AND user_id = ?",
                         (source_id, body.user_id))
        else:
            conn.execute(
                """INSERT INTO source_votes (source_id, user_id, value) VALUES (?,?,?)
                   ON CONFLICT (source_id, user_id) DO UPDATE SET value = excluded.value""",
                (source_id, body.user_id, body.value),
            )
        return _get_source(conn, source_id, body.user_id)


@router.put("/sources/{source_id}/helped", response_model=Source)
def helped(source_id: str, body: HelpedBody):
    """'This helped me understand it' (or take it back)."""
    with session() as conn:
        _require_user(conn, body.user_id)
        _get_source(conn, source_id, body.user_id)
        if body.helped:
            conn.execute(
                """INSERT INTO source_helped (source_id, user_id, created_at) VALUES (?,?,?)
                   ON CONFLICT (source_id, user_id) DO NOTHING""",
                (source_id, body.user_id, time.time()),
            )
        else:
            conn.execute("DELETE FROM source_helped WHERE source_id = ? AND user_id = ?",
                         (source_id, body.user_id))
        return _get_source(conn, source_id, body.user_id)


@router.delete("/sources/{source_id}", status_code=204)
def delete_source(source_id: str, user_id: str):
    with session() as conn:
        row = conn.execute("SELECT added_by FROM sources WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Source not found")
        if row["added_by"] != user_id:
            raise HTTPException(403, "Only the person who added a source can remove it")
        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
    return Response(status_code=204)


@router.get("/source-kinds", response_model=List[str])
def source_kinds():
    return SOURCE_KINDS


# ---------------------------------------------------------------- community


class Community(BaseModel):
    learning: int = Field(..., description="Learners working on it (rated below 100% or flagged need-to-learn)")
    understood: int
    goal_of: int = Field(..., description="Learners who pinned it as a goal")
    top_source: Optional[Source] = None


@router.get("/concepts/{concept_id}/community", response_model=Community)
def community(concept_id: str, user_id: Optional[str] = None):
    _require_concept(concept_id)
    with reader() as conn:
        learning, understood = conn.execute(
            """SELECT
                 SUM(CASE WHEN (understanding IS NOT NULL AND understanding < 100)
                           OR status = 'need_to_learn' THEN 1 ELSE 0 END),
                 SUM(CASE WHEN understanding >= 100 THEN 1 ELSE 0 END)
               FROM concept_progress WHERE concept_id = ? AND COALESCE(status, '') != 'hidden'""",
            (concept_id,),
        ).fetchone()
        goal_of = conn.execute("SELECT COUNT(*) FROM user_goals WHERE concept_id = ?", (concept_id,)).fetchone()[0]
    top = list_sources(concept_id, user_id=user_id)
    best = top[0] if top and (top[0]["score"] + top[0]["helped_count"]) > 0 else None
    return {"learning": learning or 0, "understood": understood or 0, "goal_of": goal_of, "top_source": best}


# ------------------------------------------------------------------ reports


class ReportCreate(BaseModel):
    user_id: Optional[str] = None
    kind: str
    concept_id: str
    related_concept_id: Optional[str] = None
    context_concept_id: Optional[str] = Field(None, description="The map the learner was looking at")
    message: str = Field("", max_length=2000)
    suggestion: str = Field("", max_length=2000)

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in REPORT_KINDS:
            raise ValueError(f"must be one of {REPORT_KINDS}")
        return v


class Report(BaseModel):
    id: str
    kind: str
    concept: ConceptRef
    related_concept: Optional[ConceptRef] = None
    context_concept_id: Optional[str] = None
    message: str
    suggestion: str
    status: str
    created_at: float


def _report(r: Row) -> dict:
    return {
        "id": r["id"],
        "kind": r["kind"],
        "concept": _ref(r["concept_id"]) or {"id": r["concept_id"], "name": r["concept_id"]},
        "related_concept": _ref(r["related_concept_id"]) if r["related_concept_id"] else None,
        "context_concept_id": r["context_concept_id"],
        "message": r["message"],
        "suggestion": r["suggestion"],
        "status": r["status"],
        "created_at": r["created_at"],
    }


@router.post("/reports", response_model=Report, status_code=201)
def create_report(body: ReportCreate):
    _require_concept(body.concept_id)
    if body.related_concept_id:
        _require_concept(body.related_concept_id)
    if body.kind in ("wrong_prerequisite", "missing_prerequisite", "duplicate") and not body.related_concept_id:
        raise HTTPException(422, "Pick the other concept this report is about")
    if body.kind in ("other", "wrong_description", "wrong_example") and not (body.message.strip() or body.suggestion.strip()):
        raise HTTPException(422, "Tell us what's wrong")
    rid = uuid.uuid4().hex
    with session() as conn:
        user_id = body.user_id if body.user_id and user_exists(conn, body.user_id) else None
        conn.execute(
            """INSERT INTO reports (id, user_id, kind, concept_id, related_concept_id, context_concept_id,
                                    message, suggestion, created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
            (rid, user_id, body.kind, body.concept_id, body.related_concept_id, body.context_concept_id,
             body.message.strip(), body.suggestion.strip(), time.time()),
        )
        row = conn.execute("SELECT * FROM reports WHERE id = ?", (rid,)).fetchone()
    return _report(row)


@router.get("/reports", response_model=List[Report])
def list_reports(status: str = "open", limit: int = Query(100, ge=1, le=500)):
    """For reviewing data problems (no admin auth yet — add before going public)."""
    with reader() as conn:
        rows = conn.execute(
            "SELECT * FROM reports WHERE status = ? ORDER BY created_at DESC LIMIT ?", (status, limit)
        ).fetchall()
    return [_report(r) for r in rows]


# ------------------------------------------------------------- share pages


@pages.get("/p/{concept_id}", response_class=HTMLResponse, include_in_schema=False)
def share_page(concept_id: str):
    """A public, read-only page with the learning path to a concept, so a
    teacher or friend can send "the road to X" as a plain link."""
    p = build_path(concept_id, None)
    esc = html.escape
    target = p["steps"][-1] if p["steps"] else {"name": concept_id, "description": ""}
    hours = sum(s["minutes"] for s in p["steps"]) / 60
    items = "\n".join(
        f"""<li><strong>{esc(s['name'])}</strong>
              <span class="meta">{s['minutes']} min</span>
              <p>{esc(s.get('description') or '')}</p></li>"""
        for s in p["steps"]
    )
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>The road to {esc(target['name'])} · Topolearn</title>
<meta name="description" content="{len(p['steps'])} concepts to learn, in order, to understand {esc(target['name'])}.">
<style>
 :root {{ --fg:#1b1d2a; --muted:#5b6070; --accent:#1f6feb; --bg:#f7f8fc; --card:#fff; }}
 @media (prefers-color-scheme: dark) {{ :root {{ --fg:#e8e9f0; --muted:#a2a6b5; --bg:#12131a; --card:#1c1e28; --accent:#6ea8ff; }} }}
 body {{ font: 16px/1.5 system-ui, -apple-system, Segoe UI, sans-serif; color: var(--fg); background: var(--bg);
        margin: 0; padding: 24px 16px; }}
 main {{ max-width: 720px; margin: 0 auto; }}
 h1 {{ margin: 0 0 4px; font-size: 1.8rem; }}
 .sub {{ color: var(--muted); margin-bottom: 24px; }}
 ol {{ padding-left: 1.4rem; }}
 li {{ background: var(--card); border-radius: 12px; padding: 12px 16px; margin: 0 0 10px; }}
 li p {{ margin: 4px 0 0; color: var(--muted); font-size: .95rem; }}
 .meta {{ color: var(--accent); font-size: .85rem; margin-left: 8px; }}
 footer {{ color: var(--muted); font-size: .85rem; margin-top: 24px; }}
</style></head><body><main>
<h1>The road to {esc(target['name'])}</h1>
<div class="sub">{len(p['steps'])} concepts, foundations first · about {hours:.0f} hours of study</div>
<ol>
{items}
</ol>
<footer>Made with Topolearn — learn anything by its prerequisites.</footer>
</main></body></html>"""
    return HTMLResponse(page, headers={"Cache-Control": "public, max-age=3600"})
