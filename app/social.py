"""What learners add around a lesson: thumbs up/down, saving it for later,
comments (with one level of replies and likes), problem reports, and requests
for tools that aren't in the catalog yet.

Anything done in a learner's name needs their sign-in (auth.py).
"""
import time
import uuid
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, field_validator

from . import auth, repository
from .db import Conn, get_conn
from .learning import require_lesson, require_user
from .userdb import reader, session, user_exists

router = APIRouter(prefix="/api/v1", tags=["social"])

MAX_COMMENT = 2000
REPORT_KINDS = [
    "wrong_content",        # the explanation or example is wrong
    "outdated",             # true once, not any more
    "wrong_prerequisite",   # lesson -> related lesson link shouldn't exist
    "missing_prerequisite", # lesson should require related lesson
    "broken_link",
    "typo",
    "comment",              # a comment that breaks the rules (comment_id)
    "other",
]


# ------------------------------------------------------------------ helpers


def lesson_social(conn: Conn, lesson_id: str, user_id: Optional[str]) -> dict:
    """Reactions, saved state and comment count for one lesson."""
    up, down = conn.execute(
        """SELECT COALESCE(SUM(CASE WHEN value = 1 THEN 1 ELSE 0 END), 0),
                  COALESCE(SUM(CASE WHEN value = -1 THEN 1 ELSE 0 END), 0)
           FROM lesson_reactions WHERE concept_id = ?""",
        (lesson_id,),
    ).fetchone()
    comments = conn.execute("SELECT COUNT(*) FROM comments WHERE concept_id = ?", (lesson_id,)).fetchone()[0]
    mine, saved = 0, False
    if user_id:
        row = conn.execute("SELECT value FROM lesson_reactions WHERE user_id = ? AND concept_id = ?",
                           (user_id, lesson_id)).fetchone()
        mine = row[0] if row else 0
        saved = conn.execute("SELECT 1 FROM saved_lessons WHERE user_id = ? AND concept_id = ?",
                             (user_id, lesson_id)).fetchone() is not None
    learners = conn.execute(
        "SELECT COUNT(*) FROM concept_progress WHERE concept_id = ? AND understanding >= 100", (lesson_id,)
    ).fetchone()[0]
    return {"up": up, "down": down, "mine": mine, "saved": saved, "comments": comments, "learned_by": learners}


# ---------------------------------------------------------------- reactions


class ReactionBody(BaseModel):
    user_id: str
    value: Literal[-1, 0, 1]


@router.put("/lessons/{lesson_id}/reaction")
def react(lesson_id: str, body: ReactionBody, request: Request):
    """Thumbs up (1), down (-1), or take it back (0)."""
    auth.authorize(request, body.user_id)
    require_lesson(lesson_id)
    with session() as conn:
        require_user(conn, body.user_id)
        if body.value == 0:
            conn.execute("DELETE FROM lesson_reactions WHERE user_id = ? AND concept_id = ?",
                         (body.user_id, lesson_id))
        else:
            conn.execute(
                """INSERT INTO lesson_reactions (user_id, concept_id, value, created_at) VALUES (?,?,?,?)
                   ON CONFLICT (user_id, concept_id) DO UPDATE SET value = excluded.value""",
                (body.user_id, lesson_id, body.value, time.time()),
            )
        return lesson_social(conn, lesson_id, body.user_id)


# -------------------------------------------------------------------- saved


@router.get("/users/{user_id}/saved")
def list_saved(user_id: str):
    with reader() as conn:
        require_user(conn, user_id)
        rows = conn.execute(
            "SELECT concept_id, created_at FROM saved_lessons WHERE user_id = ? ORDER BY created_at DESC LIMIT 500",
            (user_id,),
        ).fetchall()
    refs = repository.lessons_by_slug(get_conn(), [r["concept_id"] for r in rows])
    return [{**refs[r["concept_id"]], "saved_at": r["created_at"]} for r in rows if r["concept_id"] in refs]


@router.put("/users/{user_id}/saved/{lesson_id}", status_code=204)
def save(user_id: str, lesson_id: str):
    require_lesson(lesson_id)
    with session() as conn:
        require_user(conn, user_id)
        conn.execute(
            """INSERT INTO saved_lessons (user_id, concept_id, created_at) VALUES (?,?,?)
               ON CONFLICT (user_id, concept_id) DO NOTHING""",
            (user_id, lesson_id, time.time()),
        )
    return Response(status_code=204)


@router.delete("/users/{user_id}/saved/{lesson_id}", status_code=204)
def unsave(user_id: str, lesson_id: str):
    with session() as conn:
        require_user(conn, user_id)
        conn.execute("DELETE FROM saved_lessons WHERE user_id = ? AND concept_id = ?", (user_id, lesson_id))
    return Response(status_code=204)


# ----------------------------------------------------------------- comments


def _clean_body(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("write something first")
    return v


class CommentCreate(BaseModel):
    user_id: str
    body: str = Field(..., max_length=MAX_COMMENT)
    parent_id: Optional[str] = None

    @field_validator("body")
    @classmethod
    def _body(cls, v: str) -> str:
        return _clean_body(v)


class CommentEdit(BaseModel):
    user_id: str
    body: str = Field(..., max_length=MAX_COMMENT)

    @field_validator("body")
    @classmethod
    def _body(cls, v: str) -> str:
        return _clean_body(v)


class LikeBody(BaseModel):
    user_id: str
    liked: bool


_COMMENT_SELECT = """
SELECT c.id, c.concept_id, c.user_id, c.parent_id, c.body, c.created_at, c.edited_at, u.name AS author,
       (SELECT COUNT(*) FROM comment_likes l WHERE l.comment_id = c.id) AS likes,
       (SELECT COUNT(*) FROM comment_likes l WHERE l.comment_id = c.id AND l.user_id = :me) AS liked_by_me
FROM comments c JOIN users u ON u.id = c.user_id
"""


def _comment(r, me: Optional[str]) -> dict:
    return {
        "id": r["id"],
        "lesson_id": r["concept_id"],
        "parent_id": r["parent_id"],
        "author": {"id": r["user_id"], "name": r["author"]},
        "body": r["body"],
        "created_at": r["created_at"],
        "edited_at": r["edited_at"],
        "likes": r["likes"],
        "liked_by_me": bool(r["liked_by_me"]),
        "mine": me is not None and r["user_id"] == me,
    }


def _get_comment(conn: Conn, comment_id: str, me: Optional[str]) -> dict:
    r = conn.execute(_COMMENT_SELECT + " WHERE c.id = :cid", {"me": me, "cid": comment_id}).fetchone()
    if r is None:
        raise HTTPException(404, "Comment not found")
    out = _comment(r, me)
    out["replies"] = []
    return out


@router.get("/lessons/{lesson_id}/comments")
def list_comments(lesson_id: str, user_id: Optional[str] = None, sort: Literal["top", "new"] = "top",
                  limit: int = Query(50, ge=1, le=200)):
    """Top-level comments (best or newest first), each with its replies oldest first."""
    require_lesson(lesson_id)
    with reader() as conn:
        rows = conn.execute(_COMMENT_SELECT + " WHERE c.concept_id = :lid",
                            {"me": user_id, "lid": lesson_id}).fetchall()
    items = [_comment(r, user_id) for r in rows]
    top = [c for c in items if c["parent_id"] is None]
    replies: Dict[str, List[dict]] = {}
    for c in items:
        if c["parent_id"]:
            replies.setdefault(c["parent_id"], []).append(c)
    for c in top:
        c["replies"] = sorted(replies.get(c["id"], []), key=lambda x: x["created_at"])
    if sort == "top":
        top.sort(key=lambda c: (-c["likes"], -c["created_at"]))
    else:
        top.sort(key=lambda c: -c["created_at"])
    return {"total": len(items), "comments": top[:limit]}


@router.post("/lessons/{lesson_id}/comments", status_code=201)
def add_comment(lesson_id: str, body: CommentCreate, request: Request):
    auth.authorize(request, body.user_id)
    require_lesson(lesson_id)
    cid = uuid.uuid4().hex
    with session() as conn:
        require_user(conn, body.user_id)
        parent = body.parent_id
        if parent:
            row = conn.execute("SELECT concept_id, parent_id FROM comments WHERE id = ?", (parent,)).fetchone()
            if row is None or row["concept_id"] != lesson_id:
                raise HTTPException(404, "The comment you're replying to is gone")
            parent = row["parent_id"] or parent  # replies to replies join the same thread
        conn.execute(
            "INSERT INTO comments (id, concept_id, user_id, parent_id, body, created_at) VALUES (?,?,?,?,?,?)",
            (cid, lesson_id, body.user_id, parent, body.body, time.time()),
        )
        return _get_comment(conn, cid, body.user_id)


@router.patch("/comments/{comment_id}")
def edit_comment(comment_id: str, body: CommentEdit, request: Request):
    auth.authorize(request, body.user_id)
    with session() as conn:
        row = conn.execute("SELECT user_id FROM comments WHERE id = ?", (comment_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Comment not found")
        if row["user_id"] != body.user_id:
            raise HTTPException(403, "You can only edit your own comments")
        conn.execute("UPDATE comments SET body = ?, edited_at = ? WHERE id = ?", (body.body, time.time(), comment_id))
        return _get_comment(conn, comment_id, body.user_id)


@router.delete("/comments/{comment_id}", status_code=204)
def delete_comment(comment_id: str, user_id: str):
    with session() as conn:
        row = conn.execute("SELECT user_id FROM comments WHERE id = ?", (comment_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Comment not found")
        if row["user_id"] != user_id:
            raise HTTPException(403, "You can only delete your own comments")
        conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
    return Response(status_code=204)


@router.put("/comments/{comment_id}/like")
def like_comment(comment_id: str, body: LikeBody, request: Request):
    auth.authorize(request, body.user_id)
    with session() as conn:
        require_user(conn, body.user_id)
        if conn.execute("SELECT 1 FROM comments WHERE id = ?", (comment_id,)).fetchone() is None:
            raise HTTPException(404, "Comment not found")
        if body.liked:
            conn.execute(
                """INSERT INTO comment_likes (comment_id, user_id, created_at) VALUES (?,?,?)
                   ON CONFLICT (comment_id, user_id) DO NOTHING""",
                (comment_id, body.user_id, time.time()),
            )
        else:
            conn.execute("DELETE FROM comment_likes WHERE comment_id = ? AND user_id = ?", (comment_id, body.user_id))
        return _get_comment(conn, comment_id, body.user_id)


# ------------------------------------------------------------------ reports


class ReportCreate(BaseModel):
    user_id: Optional[str] = None
    kind: str
    lesson_id: str
    related_lesson_id: Optional[str] = None
    comment_id: Optional[str] = None
    message: str = Field("", max_length=2000)
    suggestion: str = Field("", max_length=2000)

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in REPORT_KINDS:
            raise ValueError(f"must be one of {REPORT_KINDS}")
        return v


def _report(r) -> dict:
    refs = repository.lessons_by_slug(get_conn(), [x for x in (r["concept_id"], r["related_concept_id"]) if x])
    return {
        "id": r["id"],
        "kind": r["kind"],
        "lesson": refs.get(r["concept_id"]) or {"id": r["concept_id"], "name": r["concept_id"]},
        "related_lesson": refs.get(r["related_concept_id"]) if r["related_concept_id"] else None,
        "comment_id": r["comment_id"],
        "message": r["message"],
        "suggestion": r["suggestion"],
        "status": r["status"],
        "created_at": r["created_at"],
    }


@router.get("/report-kinds", response_model=List[str])
def report_kinds():
    return REPORT_KINDS


@router.post("/reports", status_code=201)
def create_report(body: ReportCreate, request: Request):
    """Signed in or not. If `user_id` is given it must be the caller."""
    if body.user_id:
        auth.authorize(request, body.user_id)
    require_lesson(body.lesson_id)
    if body.related_lesson_id:
        require_lesson(body.related_lesson_id)
    if body.kind in ("wrong_prerequisite", "missing_prerequisite") and not body.related_lesson_id:
        raise HTTPException(422, "Pick the other lesson this report is about")
    if body.kind == "comment" and not body.comment_id:
        raise HTTPException(422, "Which comment?")
    if body.kind in ("other", "wrong_content", "outdated", "typo") and not (body.message.strip() or body.suggestion.strip()):
        raise HTTPException(422, "Tell us what's wrong")
    rid = uuid.uuid4().hex
    with session() as conn:
        user_id = body.user_id if body.user_id and user_exists(conn, body.user_id) else None
        conn.execute(
            """INSERT INTO reports (id, user_id, kind, concept_id, related_concept_id, comment_id, message,
                                    suggestion, created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
            (rid, user_id, body.kind, body.lesson_id, body.related_lesson_id, body.comment_id,
             body.message.strip(), body.suggestion.strip(), time.time()),
        )
        row = conn.execute("SELECT * FROM reports WHERE id = ?", (rid,)).fetchone()
    return _report(row)


@router.get("/reports")
def list_reports(status: str = "open", limit: int = Query(100, ge=1, le=500),
                 _admin: auth.Identity = Depends(auth.require_admin)):
    """For reviewing content problems. Only for the emails in ADMIN_EMAILS."""
    with reader() as conn:
        rows = conn.execute(
            "SELECT * FROM reports WHERE status = ? ORDER BY created_at DESC LIMIT ?", (status, limit)
        ).fetchall()
    return [_report(r) for r in rows]


# ------------------------------------------------------------ tool requests


class ToolRequestBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    user_id: Optional[str] = None
    anon_id: Optional[str] = Field(None, max_length=64, description="A random id the app keeps when signed out")

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("name is required")
        return v


def _request_count(conn: Conn, norm: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM tool_request_votes WHERE name_norm = ?", (norm,)).fetchone()[0]


@router.get("/tool-requests/lookup")
def lookup_tool_request(name: str = Query(..., min_length=1, max_length=80), user_id: Optional[str] = None):
    """How many learners asked for this tool (and whether you did)."""
    norm = repository.normalize(name)
    with reader() as conn:
        votes = _request_count(conn, norm)
        mine = bool(user_id) and conn.execute(
            "SELECT 1 FROM tool_request_votes WHERE name_norm = ? AND voter = ?", (norm, user_id)
        ).fetchone() is not None
    return {"name": name.strip(), "votes": votes, "requested_by_me": mine}


@router.post("/tool-requests")
def request_tool(body: ToolRequestBody, request: Request):
    """"Teach me Kubernetes": count a vote for a tool that isn't in the catalog."""
    if body.user_id:
        auth.authorize(request, body.user_id)
    voter = body.user_id or (f"anon:{body.anon_id}" if body.anon_id else None)
    if voter is None:
        raise HTTPException(422, "Sign in, or send an anon_id")
    norm = repository.normalize(body.name)
    found = repository.search_tools(get_conn(), body.name, 1)
    if found and repository.normalize(found[0]["name"]) == norm:
        raise HTTPException(409, f"{found[0]['name']} is already here")
    now = time.time()
    with session() as conn:
        conn.execute(
            """INSERT INTO tool_requests (name_norm, name, created_at, updated_at) VALUES (?,?,?,?)
               ON CONFLICT (name_norm) DO UPDATE SET updated_at = excluded.updated_at""",
            (norm, body.name, now, now),
        )
        conn.execute(
            """INSERT INTO tool_request_votes (name_norm, voter, created_at) VALUES (?,?,?)
               ON CONFLICT (name_norm, voter) DO NOTHING""",
            (norm, voter, now),
        )
        votes = _request_count(conn, norm)
    return {"name": body.name, "votes": votes, "requested_by_me": True}


@router.get("/tool-requests")
def list_tool_requests(limit: int = Query(100, ge=1, le=500), _admin: auth.Identity = Depends(auth.require_admin)):
    """Most requested first. Only for the emails in ADMIN_EMAILS."""
    with reader() as conn:
        rows = conn.execute(
            """SELECT r.name, r.name_norm, r.created_at, r.updated_at, COUNT(v.voter) AS votes
               FROM tool_requests r LEFT JOIN tool_request_votes v ON v.name_norm = r.name_norm
               GROUP BY r.name_norm ORDER BY votes DESC, r.updated_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [{"name": r["name"], "votes": r["votes"], "first_asked": r["created_at"], "last_asked": r["updated_at"]}
            for r in rows]
