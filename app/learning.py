"""A learner's journey: progress on lessons, the tools they study (and how
deep), what to learn next, spaced-repetition reviews, the home feed and stats.

Progress (per learner x lesson): understanding 0..100 (100 = learned, None =
not rated) and status None | need_to_learn | hidden ("skip, I don't need it").
A lesson is *done* when it is learned or skipped, and *ready* when everything
it requires is done.

Requests name the acting learner with `user_id`; it must be the signed-in
caller (auth.py checks it app-wide for the path and query).
"""
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Literal, Optional, Set, Tuple

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field

from . import repository
from .db import Conn, get_conn
from .userdb import reader, session, user_exists

router = APIRouter(prefix="/api/v1", tags=["learning"])

Status = Literal["need_to_learn", "hidden"]
DEFAULT_DEPTH = 2
MAX_BULK = 500
# Spaced repetition: first review a day after learning, then each correct
# answer multiplies the gap (1, 3, 7, 16, 37... days), a miss starts over.
FIRST_REVIEW_DAYS = 1.0
REVIEW_GROWTH = 2.3
MAX_REVIEW_DAYS = 180.0
DAY = 86400.0


# ----------------------------------------------------------------- helpers


def require_user(conn: Conn, user_id: str) -> None:
    if not user_exists(conn, user_id):
        raise HTTPException(404, "User not found")


def require_lesson(lesson_id: str) -> dict:
    found = repository.lessons_by_slug(get_conn(), [lesson_id]).get(lesson_id)
    if found is None:
        raise HTTPException(404, f"Unknown lesson '{lesson_id}'")
    return found


def require_tool(tool_id: str) -> dict:
    found = repository.tools_by_slug(get_conn(), [tool_id]).get(tool_id)
    if found is None:
        raise HTTPException(404, f"Unknown tool '{tool_id}'")
    return found


def progress_for(user_id: str, lesson_ids: Iterable[str]) -> Dict[str, dict]:
    ids = list(dict.fromkeys(lesson_ids))
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


def learned(p: Optional[dict]) -> bool:
    return bool(p) and (p.get("understanding") or 0) >= 100


def done(p: Optional[dict]) -> bool:
    return learned(p) or bool(p) and p.get("status") == "hidden"


def enrollments(user_id: str) -> Dict[str, dict]:
    with reader() as conn:
        return {
            r["tool_id"]: {"depth": r["depth"], "created_at": r["created_at"], "last_opened_at": r["last_opened_at"]}
            for r in conn.execute(
                "SELECT tool_id, depth, created_at, last_opened_at FROM user_tools WHERE user_id = ?", (user_id,)
            )
        }


def default_depth(user_id: Optional[str]) -> int:
    if not user_id:
        return DEFAULT_DEPTH
    with reader() as conn:
        row = conn.execute("SELECT experience FROM users WHERE id = ?", (user_id,)).fetchone()
    return 1 if row and row["experience"] == "new" else DEFAULT_DEPTH


# ------------------------------------------------------ progress through a tool


def summarize(tool_id: str, depth: int, lessons: List[dict], requires: Dict[str, List[str]],
              prog: Dict[str, dict], ext: Dict[str, dict], limit_next: int = 3) -> dict:
    """Progress through one tool down to `depth`, and the lessons to do next.

    `lessons`: the tool's lessons (any depth, study order); `requires`: direct
    prerequisites of those lessons; `prog`: the learner's progress on them and
    on their prerequisites; `ext`: other tools' lessons that are prerequisites."""
    own = [c for c in lessons if c["level"] <= depth]
    total = len(own)
    n_learned = sum(1 for c in own if learned(prog.get(c["id"])))
    n_skipped = sum(1 for c in own if not learned(prog.get(c["id"])) and done(prog.get(c["id"])))
    remaining = [c for c in own if not done(prog.get(c["id"]))]
    by_id = {c["id"]: c for c in lessons}

    def is_ready(c: dict) -> bool:
        return all(done(prog.get(r)) for r in requires.get(c["id"], []))

    ready = [c for c in remaining if is_ready(c)]
    nxt: List[dict] = []
    for c in ready:
        reason = "Needs review" if (prog.get(c["id"]) or {}).get("status") == "need_to_learn" else "Ready"
        nxt.append({**c, "reason": reason})
    # Blocked by other tools' lessons: suggest those prerequisites too.
    if len(nxt) < limit_next:
        seen = {c["id"] for c in nxt}
        for c in remaining:
            for r in requires.get(c["id"], []):
                if r in by_id or r in seen or done(prog.get(r)) or r not in ext:
                    continue
                seen.add(r)
                nxt.append({**ext[r], "reason": f"Needed for {c['name']}"})
    nxt.sort(key=lambda c: (0 if c.get("reason") == "Needs review" else 1,
                            0 if c["tool"]["id"] == tool_id else 1, c["level"], c["position"]))
    return {
        "depth": depth,
        "depth_name": repository.LEVEL_NAMES[depth],
        "total": total,
        "learned": n_learned,
        "skipped": n_skipped,
        "done": n_learned + n_skipped,
        "percent": round(100 * (n_learned + n_skipped) / total) if total else 0,
        "minutes_left": sum(c["minutes"] for c in remaining),
        "ready": len(ready),
        "next": nxt[:limit_next],
        "complete": total > 0 and not remaining,
    }


def tool_summaries(user_id: str, depths: Dict[str, int], limit_next: int = 3) -> Dict[str, dict]:
    """summarize() for several tools with as few queries as possible."""
    if not depths:
        return {}
    conn = get_conn()
    lessons = repository.tool_lessons(conn, depths)
    all_ids = [c["id"] for ls in lessons.values() for c in ls]
    requires = repository.requires_of(conn, all_ids)
    ext_ids = {r for rs in requires.values() for r in rs} - set(all_ids)
    ext = repository.lessons_by_slug(conn, ext_ids)
    prog = progress_for(user_id, [*all_ids, *ext_ids])
    return {
        tool: summarize(tool, depth, lessons.get(tool, []), requires, prog, ext, limit_next)
        for tool, depth in depths.items()
    }


# ------------------------------------------------------------------ progress


class ProgressUpdate(BaseModel):
    """Send only what changes. `null` clears a field."""

    understanding: Optional[int] = Field(None, ge=0, le=100)
    status: Optional[Status] = None


class BulkProgress(BaseModel):
    lesson_ids: List[str] = Field(..., min_length=1, max_length=MAX_BULK)
    understanding: Optional[int] = Field(None, ge=0, le=100)
    status: Optional[Status] = None


def _apply(conn: Conn, user_id: str, lesson_id: str, changes: dict, now: float) -> Tuple[Optional[int], Optional[int], Optional[str]]:
    """Write one progress change (and its event and review schedule).
    Returns (understanding before, understanding after, status after)."""
    row = conn.execute(
        "SELECT understanding, status FROM concept_progress WHERE user_id = ? AND concept_id = ?",
        (user_id, lesson_id),
    ).fetchone()
    before = row["understanding"] if row else None
    understanding = changes["understanding"] if "understanding" in changes else before
    status = changes["status"] if "status" in changes else (row["status"] if row else None)
    if understanding is None and status is None:
        conn.execute("DELETE FROM concept_progress WHERE user_id = ? AND concept_id = ?", (user_id, lesson_id))
    else:
        conn.execute(
            """INSERT INTO concept_progress (user_id, concept_id, understanding, status, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT (user_id, concept_id) DO UPDATE SET
                 understanding = excluded.understanding, status = excluded.status,
                 updated_at = excluded.updated_at""",
            (user_id, lesson_id, understanding, status, now),
        )
    if (before, row["status"] if row else None) != (understanding, status):
        conn.execute(
            "INSERT INTO progress_events (user_id, concept_id, understanding, status, at) VALUES (?,?,?,?,?)",
            (user_id, lesson_id, understanding, status, now),
        )
    if (understanding or 0) >= 100 and status != "hidden":
        conn.execute(
            """INSERT INTO reviews (user_id, concept_id, due_at, interval_days) VALUES (?,?,?,?)
               ON CONFLICT (user_id, concept_id) DO NOTHING""",
            (user_id, lesson_id, now + FIRST_REVIEW_DAYS * DAY, FIRST_REVIEW_DAYS),
        )
    else:
        conn.execute("DELETE FROM reviews WHERE user_id = ? AND concept_id = ?", (user_id, lesson_id))
    return before, understanding, status


def _newly_ready(user_id: str, lesson_id: str) -> List[dict]:
    """Lessons that need `lesson_id` and now have everything they need."""
    conn = get_conn()
    dependents = [d["id"] for d in (repository.get_lesson(conn, lesson_id) or {}).get("unlocks", [])]
    if not dependents:
        return []
    needs = repository.requires_of(conn, dependents)
    refs = repository.lessons_by_slug(conn, dependents)
    prog = progress_for(user_id, {lesson_id, *dependents, *(r for rs in needs.values() for r in rs)})
    out = []
    for d in dependents:
        if d in refs and not done(prog.get(d)) and all(done(prog.get(r)) for r in needs[d]):
            out.append(refs[d])
    return out[:5]


@router.get("/users/{user_id}/progress")
def list_progress(user_id: str, status: Optional[Status] = Query(None), tool: Optional[str] = Query(None)):
    """The learner's marks, newest first; optionally only one status or one tool."""
    with reader() as conn:
        require_user(conn, user_id)
        sql = "SELECT concept_id, understanding, status, updated_at FROM concept_progress WHERE user_id = ?"
        args: list = [user_id]
        if status:
            sql += " AND status = ?"
            args.append(status)
        if tool:
            sql += " AND concept_id LIKE ?"
            args.append(tool.replace("%", "").replace("_", "\\_") + ".%")
        rows = conn.execute(sql + " ORDER BY updated_at DESC LIMIT 1000", args).fetchall()
    refs = repository.lessons_by_slug(get_conn(), [r["concept_id"] for r in rows])
    return [
        {"lesson": refs[r["concept_id"]], "understanding": r["understanding"], "status": r["status"],
         "updated_at": r["updated_at"]}
        for r in rows if r["concept_id"] in refs
    ]


@router.put("/users/{user_id}/progress/{lesson_id}")
def set_progress(user_id: str, lesson_id: str, body: ProgressUpdate):
    lesson = require_lesson(lesson_id)
    changes = body.model_dump(exclude_unset=True)
    with session() as conn:
        require_user(conn, user_id)
        before, understanding, status = _apply(conn, user_id, lesson_id, changes, time.time())
    just_learned = (understanding or 0) >= 100 and (before or 0) < 100
    finished_tool = None
    if just_learned:
        with reader() as conn:
            row = conn.execute("SELECT depth FROM user_tools WHERE user_id = ? AND tool_id = ?",
                               (user_id, lesson["tool"]["id"])).fetchone()
        if row:
            s = tool_summaries(user_id, {lesson["tool"]["id"]: row["depth"]})[lesson["tool"]["id"]]
            if s["complete"] and lesson["level"] <= row["depth"]:
                finished_tool = {**lesson["tool"], "depth": row["depth"],
                                 "depth_name": repository.LEVEL_NAMES[row["depth"]]}
    return {
        "lesson_id": lesson_id,
        "understanding": understanding,
        "status": status,
        "newly_ready": _newly_ready(user_id, lesson_id) if just_learned else [],
        "finished_tool": finished_tool,
    }


@router.delete("/users/{user_id}/progress/{lesson_id}", status_code=204)
def clear_progress(user_id: str, lesson_id: str):
    with session() as conn:
        require_user(conn, user_id)
        _apply(conn, user_id, lesson_id, {"understanding": None, "status": None}, time.time())
    return Response(status_code=204)


@router.post("/users/{user_id}/progress/bulk")
def bulk_progress(user_id: str, body: BulkProgress):
    """Mark many lessons at once, e.g. "I already know everything below this"."""
    changes = body.model_dump(exclude_unset=True, exclude={"lesson_ids"})
    if not changes:
        raise HTTPException(422, "Send understanding and/or status")
    known = repository.lessons_by_slug(get_conn(), body.lesson_ids)
    unknown = [i for i in body.lesson_ids if i not in known]
    if unknown:
        raise HTTPException(422, f"Unknown lesson id(s): {', '.join(unknown[:5])}")
    now = time.time()
    with session() as conn:
        require_user(conn, user_id)
        for lesson_id in dict.fromkeys(body.lesson_ids):
            _apply(conn, user_id, lesson_id, changes, now)
    return {"updated": len(set(body.lesson_ids))}


# ------------------------------------------------------------- enrolled tools


class Enrollment(BaseModel):
    depth: int = Field(..., ge=1, le=4)


class KnownLevel(BaseModel):
    level: int = Field(..., ge=1, le=4, description="Mark every lesson down to this depth as learned")


def _enrolled_tool(user_id: str, tool_id: str, depth: int, enrolled: dict) -> dict:
    tool = require_tool(tool_id)
    summary = tool_summaries(user_id, {tool_id: depth})[tool_id]
    return {"tool": tool, **summary, "enrolled_at": enrolled.get("created_at"),
            "last_opened_at": enrolled.get("last_opened_at")}


@router.get("/users/{user_id}/tools")
def list_enrolled(user_id: str):
    """Tools the learner studies, most recently used first, with progress and next lessons."""
    with reader() as conn:
        require_user(conn, user_id)
    mine = enrollments(user_id)
    tools = repository.tools_by_slug(get_conn(), mine)
    summaries = tool_summaries(user_id, {t: e["depth"] for t, e in mine.items() if t in tools})
    out = [
        {"tool": tools[t], **summaries[t], "enrolled_at": e["created_at"], "last_opened_at": e["last_opened_at"]}
        for t, e in mine.items() if t in tools
    ]
    out.sort(key=lambda x: -(x["last_opened_at"] or 0))
    return out


@router.put("/users/{user_id}/tools/{tool_id}")
def enroll(user_id: str, tool_id: str, body: Enrollment):
    """Start a tool, or change how deep you want to go."""
    require_tool(tool_id)
    now = time.time()
    with session() as conn:
        require_user(conn, user_id)
        conn.execute(
            """INSERT INTO user_tools (user_id, tool_id, depth, created_at, last_opened_at) VALUES (?,?,?,?,?)
               ON CONFLICT (user_id, tool_id) DO UPDATE SET depth = excluded.depth,
                 last_opened_at = excluded.last_opened_at""",
            (user_id, tool_id, body.depth, now, now),
        )
    return _enrolled_tool(user_id, tool_id, body.depth, enrollments(user_id).get(tool_id, {}))


@router.delete("/users/{user_id}/tools/{tool_id}", status_code=204)
def unenroll(user_id: str, tool_id: str):
    """Stop studying a tool (progress on its lessons is kept)."""
    with session() as conn:
        require_user(conn, user_id)
        conn.execute("DELETE FROM user_tools WHERE user_id = ? AND tool_id = ?", (user_id, tool_id))
    return Response(status_code=204)


@router.get("/users/{user_id}/tools/{tool_id}/next")
def next_lessons(user_id: str, tool_id: str, depth: Optional[int] = Query(None, ge=1, le=4),
                 limit: int = Query(3, ge=1, le=20)):
    require_tool(tool_id)
    with reader() as conn:
        require_user(conn, user_id)
    d = depth or enrollments(user_id).get(tool_id, {}).get("depth") or default_depth(user_id)
    return tool_summaries(user_id, {tool_id: d}, limit_next=limit)[tool_id]["next"]


@router.post("/users/{user_id}/tools/{tool_id}/known")
def mark_tool_known(user_id: str, tool_id: str, body: KnownLevel):
    """"I already know Python essentials": every lesson of the tool down to
    that level counts as learned (lessons already learned are left alone)."""
    require_tool(tool_id)
    lessons = [c["id"] for c in repository.tool_lessons(get_conn(), [tool_id])[tool_id] if c["level"] <= body.level]
    prog = progress_for(user_id, lessons)
    todo = [i for i in lessons if not learned(prog.get(i))]
    now = time.time()
    with session() as conn:
        require_user(conn, user_id)
        for lesson_id in todo:
            _apply(conn, user_id, lesson_id, {"understanding": 100, "status": None}, now)
    return {"updated": len(todo)}


# ------------------------------------------------------------------- reviews


class ReviewResult(BaseModel):
    correct: bool


@router.get("/users/{user_id}/reviews")
def due_reviews(user_id: str, limit: int = Query(10, ge=1, le=50)):
    """Learned lessons whose quiz is due again, most overdue first."""
    with reader() as conn:
        require_user(conn, user_id)
        rows = conn.execute(
            """SELECT concept_id, due_at, interval_days, reps FROM reviews
               WHERE user_id = ? AND due_at <= ? ORDER BY due_at LIMIT ?""",
            (user_id, time.time(), limit * 2),
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM reviews WHERE user_id = ? AND due_at <= ?",
                             (user_id, time.time())).fetchone()[0]
    out = []
    conn = get_conn()
    for r in rows:
        lesson = repository.get_lesson(conn, r["concept_id"])
        if lesson and lesson["quiz"]:
            keep = {k: lesson[k] for k in ("id", "name", "level", "minutes", "summary", "tool", "takeaways", "quiz")}
            out.append({**keep, "due_at": r["due_at"], "interval_days": r["interval_days"], "reps": r["reps"]})
        if len(out) >= limit:
            break
    return {"due": total, "items": out}


@router.post("/users/{user_id}/reviews/{lesson_id}")
def record_review(user_id: str, lesson_id: str, body: ReviewResult):
    """Answered the review quiz: right pushes the next review further out, wrong brings it back tomorrow."""
    require_lesson(lesson_id)
    now = time.time()
    with session() as conn:
        require_user(conn, user_id)
        row = conn.execute(
            "SELECT interval_days, reps, lapses FROM reviews WHERE user_id = ? AND concept_id = ? FOR UPDATE",
            (user_id, lesson_id),
        ).fetchone()
        interval = row["interval_days"] if row else FIRST_REVIEW_DAYS
        reps = row["reps"] if row else 0
        lapses = row["lapses"] if row else 0
        if body.correct:
            interval = min(MAX_REVIEW_DAYS, max(FIRST_REVIEW_DAYS, interval) * (REVIEW_GROWTH if reps else 3.0))
            reps += 1
        else:
            interval = FIRST_REVIEW_DAYS
            lapses += 1
        conn.execute(
            """INSERT INTO reviews (user_id, concept_id, due_at, interval_days, reps, lapses) VALUES (?,?,?,?,?,?)
               ON CONFLICT (user_id, concept_id) DO UPDATE SET due_at = excluded.due_at,
                 interval_days = excluded.interval_days, reps = excluded.reps, lapses = excluded.lapses""",
            (user_id, lesson_id, now + interval * DAY, interval, reps, lapses),
        )
    return {"lesson_id": lesson_id, "due_at": now + interval * DAY, "interval_days": round(interval, 1)}


# ------------------------------------------------------------- activity & stats


def _activity(user_id: str, tz: timezone) -> dict:
    """Streaks and per-day / per-week counts of lessons learned (first time at 100%)."""
    with reader() as conn:
        events = conn.execute(
            "SELECT concept_id, understanding, at FROM progress_events WHERE user_id = ? ORDER BY at",
            (user_id,),
        ).fetchall()
    today = datetime.now(tz).date()
    days = sorted({datetime.fromtimestamp(e["at"], tz).date() for e in events})
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
    first_learned: Dict[str, datetime] = {}
    for e in events:
        if (e["understanding"] or 0) >= 100 and e["concept_id"] not in first_learned:
            first_learned[e["concept_id"]] = datetime.fromtimestamp(e["at"], tz)
    this_monday = today - timedelta(days=today.weekday())
    weeks = [this_monday - timedelta(weeks=i) for i in range(7, -1, -1)]
    per_week = {w: 0 for w in weeks}
    last_7 = [today - timedelta(days=i) for i in range(6, -1, -1)]
    per_day = {d: 0 for d in last_7}
    for when in first_learned.values():
        d = when.date()
        w = d - timedelta(days=d.weekday())
        if w in per_week:
            per_week[w] += 1
        if d in per_day:
            per_day[d] += 1
    return {
        "streak_days": current,
        "best_streak": best,
        "active_today": bool(days) and days[-1] == today,
        "learned_today": per_day[today],
        "learned_this_week": per_week[this_monday],
        "weekly": [{"week_start": w.isoformat(), "learned": per_week[w]} for w in weeks],
        "daily": [{"day": d.isoformat(), "learned": per_day[d]} for d in last_7],
    }


@router.get("/users/{user_id}/stats")
def stats(user_id: str, tz_offset_minutes: int = Query(0, ge=-14 * 60, le=14 * 60)):
    """`tz_offset_minutes`: the device's UTC offset, so "today" matches the learner's day."""
    tz = timezone(timedelta(minutes=tz_offset_minutes))
    with reader() as conn:
        require_user(conn, user_id)
        counts = conn.execute(
            """SELECT
                 SUM(CASE WHEN understanding >= 100 THEN 1 ELSE 0 END),
                 SUM(CASE WHEN understanding > 0 AND understanding < 100 THEN 1 ELSE 0 END),
                 SUM(CASE WHEN status = 'need_to_learn' THEN 1 ELSE 0 END),
                 SUM(CASE WHEN status = 'hidden' THEN 1 ELSE 0 END)
               FROM concept_progress WHERE user_id = ?""",
            (user_id,),
        ).fetchone()
        minutes = conn.execute(
            """SELECT COALESCE(SUM(c.minutes), 0) FROM concept_progress p
               JOIN graph.concepts c ON c.slug = p.concept_id
               WHERE p.user_id = ? AND p.understanding >= 100""",
            (user_id,),
        ).fetchone()[0]
        goal = conn.execute("SELECT daily_goal FROM users WHERE id = ?", (user_id,)).fetchone()[0]
    mine = enrollments(user_id)
    tools = repository.tools_by_slug(get_conn(), mine)
    summaries = tool_summaries(user_id, {t: e["depth"] for t, e in mine.items() if t in tools}, limit_next=0)
    return {
        "learned": counts[0] or 0,
        "partly": counts[1] or 0,
        "need_to_learn": counts[2] or 0,
        "skipped": counts[3] or 0,
        "minutes_learned": int(minutes),
        "daily_goal": goal,
        **_activity(user_id, tz),
        "tools": sorted(
            [{"tool": tools[t], **{k: v for k, v in s.items() if k != "next"}} for t, s in summaries.items()],
            key=lambda x: -x["percent"],
        ),
    }


@router.get("/users/{user_id}/feed")
def feed(user_id: str, tz_offset_minutes: int = Query(0, ge=-14 * 60, le=14 * 60)):
    """Everything the home screen needs: today's goal and streak, the tools in
    progress with their next lessons, reviews due, saved lessons, and what
    other learners are starting this week."""
    tz = timezone(timedelta(minutes=tz_offset_minutes))
    with reader() as conn:
        require_user(conn, user_id)
        goal = conn.execute("SELECT daily_goal FROM users WHERE id = ?", (user_id,)).fetchone()[0]
        saved_rows = conn.execute(
            "SELECT concept_id FROM saved_lessons WHERE user_id = ? ORDER BY created_at DESC LIMIT 6", (user_id,)
        ).fetchall()
        due = conn.execute("SELECT COUNT(*) FROM reviews WHERE user_id = ? AND due_at <= ?",
                           (user_id, time.time())).fetchone()[0]
        trending_rows = conn.execute(
            """SELECT tool_id, COUNT(*) AS n FROM user_tools WHERE created_at >= ?
               GROUP BY tool_id ORDER BY n DESC LIMIT 12""",
            (time.time() - 7 * DAY,),
        ).fetchall()
    mine = enrollments(user_id)
    conn = get_conn()
    tools = repository.tools_by_slug(conn, [*mine, *(r["tool_id"] for r in trending_rows)])
    summaries = tool_summaries(user_id, {t: e["depth"] for t, e in mine.items() if t in tools})
    learning = [
        {"tool": tools[t], **summaries[t], "last_opened_at": e["last_opened_at"]}
        for t, e in mine.items() if t in tools
    ]
    learning.sort(key=lambda x: (x["complete"], -(x["last_opened_at"] or 0)))

    up_next: List[dict] = []
    seen: Set[str] = set()
    for item in learning:  # round-robin would scatter attention; favour the most recent tool
        for n in item["next"]:
            if n["id"] not in seen and len(up_next) < 4:
                seen.add(n["id"])
                up_next.append(n)

    saved = repository.lessons_by_slug(conn, [r["concept_id"] for r in saved_rows])
    trending = [
        {**tools[r["tool_id"]], "learners": r["n"]}
        for r in trending_rows if r["tool_id"] in tools and r["tool_id"] not in mine
    ][:6]
    if len(trending) < 6:  # quiet week: fill with the catalog's top tools
        have = {t["id"] for t in trending} | set(mine)
        for t in repository.list_tools(conn):
            if t["id"] not in have and len(trending) < 6:
                trending.append({**{k: t[k] for k in ("id", "name", "category", "tagline", "icon", "color")},
                                 "learners": 0})
    activity = _activity(user_id, tz)
    return {
        "today": {
            "learned": activity["learned_today"],
            "goal": goal,
            "streak_days": activity["streak_days"],
            "active_today": activity["active_today"],
            "daily": activity["daily"],
        },
        "learning": learning,
        "up_next": up_next,
        "reviews_due": due,
        "saved": [saved[r["concept_id"]] for r in saved_rows if r["concept_id"] in saved],
        "trending": trending,
    }
