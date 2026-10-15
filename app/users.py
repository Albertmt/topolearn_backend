"""Users (accounts and onboarding profile), stored in Postgres with the rest.

There is no real authentication yet (dummy "pick a user" login). When Google
sign-in is added, store the Google subject id in `auth_subject` and look users
up by it; everything else stays the same.
"""
import json
import time
import uuid
from datetime import date
from typing import Annotated, List, Optional

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import AfterValidator, BaseModel, Field, field_validator

from . import repository, userdb
from .db import Conn, Row, get_conn
from .userdb import reader as _users_reader
from .userdb import session as _users_db

router = APIRouter(prefix="/api/v1", tags=["users"])

EDUCATION_LEVELS = [
    "primary_school",
    "middle_school",
    "high_school",
    "some_college",
    "bachelors",
    "masters",
    "doctorate",
    "self_taught",
    "other",
]

# Shown as quick picks during onboarding; must be ids in the concept DB.
SUGGESTED_DISCIPLINES = [
    "machine_learning", "deep_learning", "data_science", "artificial_intelligence",
    "natural_language_processing", "computer_vision", "generative_ai", "statistics",
    "probability_theory", "linear_algebra", "calculus", "algorithms",
    "reinforcement_learning", "data_visualization", "causal_inference", "robotics",
]

# ------------------------------------------------------------------ schemas


class Discipline(BaseModel):
    id: str
    name: str
    type: Optional[str] = None


class User(BaseModel):
    id: str
    name: str
    email: Optional[str] = None
    birth_year: Optional[int] = None
    age: Optional[int] = None
    education_level: Optional[str] = None
    interests: List[Discipline] = []
    onboarded: bool = False
    created_at: float


def _clean_email(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    v = v.strip().lower()
    if not v:
        return None
    if "@" not in v or "." not in v.split("@")[-1] or " " in v:
        raise ValueError("not a valid email address")
    return v


Email = Annotated[Optional[str], Field(None, max_length=200), AfterValidator(_clean_email)]


class UserCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    email: Email = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("name is required")
        return v


class UserUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    email: Email = None
    age: Optional[int] = Field(None, ge=3, le=120)
    education_level: Optional[str] = None
    interests: Optional[List[str]] = Field(None, max_length=50)
    onboarded: Optional[bool] = None

    @field_validator("education_level")
    @classmethod
    def _edu(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in EDUCATION_LEVELS:
            raise ValueError(f"must be one of {EDUCATION_LEVELS}")
        return v


# ------------------------------------------------------------------ helpers


def _to_user(row: Row) -> dict:
    ids = json.loads(row["interests"] or "[]")
    found = repository.concepts_by_slug(get_conn(), ids)
    interests = []
    for cid in ids:
        c = found.get(cid)
        if c:  # silently skip ids that disappeared in a data rebuild
            interests.append({"id": c["id"], "name": c["name"], "type": c["type"]})
    by = row["birth_year"]
    return {
        "id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "birth_year": by,
        "age": (date.today().year - by) if by else None,
        "education_level": row["education_level"],
        "interests": interests,
        "onboarded": bool(row["onboarded"]),
        "created_at": row["created_at"],
    }


def _get_row(conn: Conn, user_id: str) -> Row:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "User not found")
    return row


# ------------------------------------------------------------------- routes


@router.get("/users", response_model=List[User])
def list_users():
    with _users_reader() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY updated_at DESC").fetchall()
    return [_to_user(r) for r in rows]


@router.post("/users", response_model=User, status_code=201)
def create_user(body: UserCreate):
    now = time.time()
    uid = uuid.uuid4().hex
    try:
        with _users_db() as conn:
            conn.execute(
                "INSERT INTO users (id, name, email, created_at, updated_at) VALUES (?,?,?,?,?)",
                (uid, body.name, body.email, now, now),
            )
            row = _get_row(conn, uid)
    except userdb.IntegrityError:
        raise HTTPException(409, "A user with this email already exists")
    return _to_user(row)


@router.get("/users/{user_id}", response_model=User)
def get_user(user_id: str):
    with _users_reader() as conn:
        return _to_user(_get_row(conn, user_id))


@router.patch("/users/{user_id}", response_model=User)
def update_user(user_id: str, body: UserUpdate):
    fields = body.model_dump(exclude_unset=True)
    sets, values = [], []
    if "name" in fields:
        name = " ".join((fields["name"] or "").split())
        if not name:
            raise HTTPException(422, "name is required")
        sets.append("name = ?"), values.append(name)
    if "email" in fields:
        sets.append("email = ?"), values.append(fields["email"])
    if "age" in fields:
        age = fields["age"]
        sets.append("birth_year = ?"), values.append(date.today().year - age if age else None)
    if "education_level" in fields:
        sets.append("education_level = ?"), values.append(fields["education_level"])
    if "interests" in fields:
        ids = list(dict.fromkeys(fields["interests"] or []))  # de-dupe, keep order
        known = repository.concepts_by_slug(get_conn(), ids)
        unknown = [i for i in ids if i not in known]
        if unknown:
            raise HTTPException(422, f"Unknown concept id(s): {', '.join(unknown[:5])}")
        sets.append("interests = ?"), values.append(json.dumps(ids))
    if "onboarded" in fields:
        sets.append("onboarded = ?"), values.append(1 if fields["onboarded"] else 0)

    try:
        with _users_db() as conn:
            _get_row(conn, user_id)
            if sets:
                sets.append("updated_at = ?")
                values.append(time.time())
                conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", (*values, user_id))
            row = _get_row(conn, user_id)
    except userdb.IntegrityError:
        raise HTTPException(409, "A user with this email already exists")
    return _to_user(row)


@router.delete("/users/{user_id}", status_code=204)
def delete_user(user_id: str):
    with _users_db() as conn:
        _get_row(conn, user_id)
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return Response(status_code=204)


@router.get("/disciplines", response_model=List[Discipline])
def disciplines(
    q: Optional[str] = Query(None, max_length=100),
    limit: int = Query(12, ge=1, le=50),
):
    """No query: the onboarding quick picks. With a query: matching fields of
    study first, then any other matching concept."""
    conn = get_conn()
    if not q or not q.strip():
        out = []
        found = repository.concepts_by_slug(conn, SUGGESTED_DISCIPLINES)
        for cid in SUGGESTED_DISCIPLINES:
            c = found.get(cid)
            if c:
                out.append({"id": c["id"], "name": c["name"], "type": c["type"]})
        return out[:limit]
    found = repository.search(conn, q, min(50, limit * 3))
    found.sort(key=lambda c: 0 if c["type"] == "field" else 1)  # stable: keeps relevance order
    return [{"id": c["id"], "name": c["name"], "type": c["type"]} for c in found[:limit]]


@router.get("/education-levels", response_model=List[str])
def education_levels():
    return EDUCATION_LEVELS
