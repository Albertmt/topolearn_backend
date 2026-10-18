"""Users (accounts and their learning preferences), stored in Postgres with the rest.

Accounts come from Firebase sign-in (Google, Facebook, Apple): the first
`POST /api/v1/session` with a valid ID token creates the account, keyed by the
Firebase uid (`auth_provider = 'firebase'`, `auth_subject = uid`). See auth.py.
"""
import time
import uuid
from typing import Annotated, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import AfterValidator, BaseModel, Field, field_validator

from . import auth, userdb
from .db import Conn, Row
from .userdb import reader as _users_reader
from .userdb import session as _users_db

router = APIRouter(prefix="/api/v1", tags=["users"])

EXPERIENCE = ["new", "some", "pro"]  # never programmed / some programming / professional

# ------------------------------------------------------------------ schemas


class User(BaseModel):
    id: str
    name: str
    email: Optional[str] = None
    experience: Optional[str] = None
    daily_goal: int = 2
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


class UserUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    email: Email = None
    experience: Optional[str] = None
    daily_goal: Optional[int] = Field(None, ge=1, le=20)
    onboarded: Optional[bool] = None

    @field_validator("experience")
    @classmethod
    def _exp(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in EXPERIENCE:
            raise ValueError(f"must be one of {EXPERIENCE}")
        return v


# ------------------------------------------------------------------ helpers


def _to_user(row: Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "experience": row["experience"],
        "daily_goal": row["daily_goal"],
        "onboarded": bool(row["onboarded"]),
        "created_at": row["created_at"],
    }


def _get_row(conn: Conn, user_id: str) -> Row:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "User not found")
    return row


# ------------------------------------------------------------------- routes


def _name_for(ident: auth.Identity) -> str:
    name = " ".join((ident.name or "").split())[:80].strip()
    if not name and ident.email:
        name = ident.email.split("@")[0][:80]
    return name or "Learner"


@router.post("/session", response_model=User)
def start_session(request: Request):
    """Call right after signing in (and on every app start): returns the
    signed-in learner's account, creating it on their first sign-in from the
    name and email their Google/Facebook/Apple account shares."""
    ident = auth.identity(request)
    with _users_db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE auth_provider = 'firebase' AND auth_subject = ?", (ident.uid,)
        ).fetchone()
        if row is None:
            now = time.time()
            uid = uuid.uuid4().hex
            email = ident.email
            if email and conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
                email = None  # someone else already uses it; they can add another in Settings
            try:
                with _users_db() as sp:  # savepoint: a lost race on the email must not end the transaction
                    sp.execute(
                        """INSERT INTO users (id, name, email, auth_provider, auth_subject, created_at, updated_at)
                           VALUES (?,?,?,'firebase',?,?,?)
                           ON CONFLICT (auth_provider, auth_subject) WHERE auth_subject IS NOT NULL DO NOTHING""",
                        (uid, _name_for(ident), email, ident.uid, now, now),
                    )
            except userdb.IntegrityError:
                conn.execute(
                    """INSERT INTO users (id, name, email, auth_provider, auth_subject, created_at, updated_at)
                       VALUES (?,?,NULL,'firebase',?,?,?)
                       ON CONFLICT (auth_provider, auth_subject) WHERE auth_subject IS NOT NULL DO NOTHING""",
                    (uid, _name_for(ident), ident.uid, now, now),
                )
            # Two first sign-ins at once: whichever insert won, read it back.
            row = conn.execute(
                "SELECT * FROM users WHERE auth_provider = 'firebase' AND auth_subject = ?", (ident.uid,)
            ).fetchone()
    auth.remember(ident.uid, row["id"])
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
    if "experience" in fields:
        sets.append("experience = ?"), values.append(fields["experience"])
    if "daily_goal" in fields and fields["daily_goal"] is not None:
        sets.append("daily_goal = ?"), values.append(fields["daily_goal"])
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
    """Delete the account and everything in it (progress, tools, reviews,
    reactions, saved lessons, comments and the replies under them, likes, tool
    request votes, history), and the Firebase sign-in record. Reports stay,
    without their id, so the content can still be fixed."""
    with _users_db() as conn:
        row = _get_row(conn, user_id)
        conn.execute("DELETE FROM tool_request_votes WHERE voter = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    auth.forget(user_id)
    if row["auth_provider"] == "firebase" and row["auth_subject"]:
        auth.delete_firebase_account(row["auth_subject"])
    return Response(status_code=204)
