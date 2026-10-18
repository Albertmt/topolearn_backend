"""Sign-in with Firebase Authentication (Google, Facebook, Apple).

The app signs the learner in through Firebase and sends the resulting ID token
with every request:

    Authorization: Bearer <Firebase ID token>

We check the token's signature against Google's published keys, then map the
Firebase user (the token's `sub`) to our own user id. `POST /api/v1/session`
creates the account on the first sign-in.

Anything that reads or changes a learner's data names them with a `user_id`
(in the path, the query string or the body). Those requests only succeed for
the signed-in learner themselves; see `require_self` and `authorize`.
"""
import logging
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

import jwt
from fastapi import HTTPException, Request

from . import config
from .userdb import reader

log = logging.getLogger(__name__)

# Google's public keys for Firebase ID tokens, as a JWK set. Cached in memory and
# refreshed when a token names a key we haven't seen (Google rotates them daily).
JWKS_URL = "https://www.googleapis.com/service_accounts/v1/jwk/securetoken@system.gserviceaccount.com"
_jwks = jwt.PyJWKClient(JWKS_URL, cache_keys=True, lifespan=6 * 3600, timeout=10)

_LEEWAY = 30  # seconds of clock skew we tolerate

SIGN_IN = "Sign in to continue."
EXPIRED = "Your sign-in has expired. Sign in again."
INVALID = "Your sign-in isn't valid. Sign in again."
NO_ACCOUNT = "There's no account for this sign-in yet. Sign in again."
NOT_YOURS = "You can only see and change your own data."


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(401, detail, headers={"WWW-Authenticate": "Bearer"})


@dataclass(frozen=True)
class Identity:
    """Who a valid token says the caller is."""

    uid: str                    # Firebase user id (stable per person and project)
    name: Optional[str] = None
    email: Optional[str] = None
    email_verified: bool = False
    provider: Optional[str] = None  # "google.com", "facebook.com", "apple.com", ...


# ------------------------------------------------------------ token checks


def _verify_firebase_jwt(token: str) -> dict:
    """The token's claims, if Google signed it for our Firebase project and it
    hasn't expired. Follows https://firebase.google.com/docs/auth/admin/verify-id-tokens."""
    project = config.FIREBASE_PROJECT_ID
    if not project:
        raise HTTPException(503, "Sign-in isn't set up on this server (FIREBASE_PROJECT_ID is missing).")
    try:
        key = _jwks.get_signing_key_from_jwt(token)
    except jwt.PyJWKClientConnectionError:
        log.warning("couldn't fetch Firebase signing keys", exc_info=True)
        raise HTTPException(503, "Couldn't check your sign-in right now. Try again in a moment.")
    except (jwt.PyJWKClientError, jwt.InvalidTokenError):
        raise _unauthorized(INVALID)
    try:
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            audience=project,
            issuer=f"https://securetoken.google.com/{project}",
            leeway=_LEEWAY,
            options={"require": ["exp", "iat", "aud", "iss", "sub", "auth_time"]},
        )
    except jwt.ExpiredSignatureError:
        raise _unauthorized(EXPIRED)
    except jwt.InvalidTokenError:
        raise _unauthorized(INVALID)
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub or len(sub) > 128:
        raise _unauthorized(INVALID)
    auth_time = claims.get("auth_time")
    if not isinstance(auth_time, (int, float)) or auth_time > time.time() + _LEEWAY:
        raise _unauthorized(INVALID)
    return claims


def verify_token(token: str) -> Identity:
    """Check a Firebase ID token. Raises 401 if it isn't valid. (Tests replace this.)"""
    claims = _verify_firebase_jwt(token)
    firebase = claims.get("firebase") if isinstance(claims.get("firebase"), dict) else {}
    name = claims.get("name")
    email = claims.get("email")
    return Identity(
        uid=claims["sub"],
        name=name.strip() or None if isinstance(name, str) else None,
        email=email.strip().lower() or None if isinstance(email, str) else None,
        email_verified=claims.get("email_verified") is True,
        provider=firebase.get("sign_in_provider"),
    )


def bearer_token(request: Request) -> Optional[str]:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    token = token.strip()
    return token if scheme.lower() == "bearer" and token else None


def identity(request: Request) -> Identity:
    """The signed-in caller, or 401. Checked once per request."""
    cached = getattr(request.state, "identity", None)
    if cached is not None:
        return cached
    token = bearer_token(request)
    if token is None:
        raise _unauthorized(SIGN_IN)
    ident = verify_token(token)
    request.state.identity = ident
    return ident


# ------------------------------------------------- Firebase user -> our user

# Firebase uid -> our user id. Saves a database round trip on most requests.
# Only ever trusted when it agrees with what the request asks for; anything
# else is looked up again (another server may have deleted or re-created the
# account since).
_user_ids: Dict[str, str] = {}
_lock = threading.Lock()
_MAX_CACHED = 50_000


def remember(uid: str, user_id: str) -> None:
    with _lock:
        if len(_user_ids) >= _MAX_CACHED:
            _user_ids.clear()
        _user_ids[uid] = user_id


def forget(user_id: Optional[str] = None) -> None:
    """Drop a deleted user from the cache (or everything, with no argument)."""
    with _lock:
        if user_id is None:
            _user_ids.clear()
        else:
            for uid in [u for u, mine in _user_ids.items() if mine == user_id]:
                del _user_ids[uid]


def lookup_user_id(uid: str) -> Optional[str]:
    """Our user id for a Firebase user, straight from the database."""
    with reader() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE auth_provider = 'firebase' AND auth_subject = ?", (uid,)
        ).fetchone()
    if row is None:
        forget_uid(uid)
        return None
    remember(uid, row[0])
    return row[0]


def forget_uid(uid: str) -> None:
    with _lock:
        _user_ids.pop(uid, None)


def current_user_id(request: Request, expected: Optional[str] = None) -> str:
    """Our id for the signed-in caller, or 401 if they aren't signed in or have no account."""
    ident = identity(request)
    cached = _user_ids.get(ident.uid)
    if cached is not None and (expected is None or cached == expected):
        return cached
    user_id = lookup_user_id(ident.uid)
    if user_id is None:
        raise _unauthorized(NO_ACCOUNT)
    return user_id


def authorize(request: Request, user_id: str) -> str:
    """Allow the request only if `user_id` is the signed-in caller."""
    if current_user_id(request, expected=user_id) != user_id:
        raise HTTPException(403, NOT_YOURS)
    return user_id


def optional_user_id(request: Request) -> Optional[str]:
    """The caller's id if they sent a valid sign-in, else None (for endpoints
    that also work signed out). A token that is present but invalid still fails."""
    if bearer_token(request) is None:
        return None
    return current_user_id(request)


def require_self(request: Request) -> None:
    """App-wide guard: a `user_id` in the path or query must be the caller."""
    claimed = request.path_params.get("user_id") or request.query_params.get("user_id")
    if claimed:
        authorize(request, claimed)


def require_admin(request: Request) -> Identity:
    """Only the addresses in ADMIN_EMAILS, signed in with a verified email."""
    ident = identity(request)
    if not (ident.email and ident.email_verified and ident.email in config.ADMIN_EMAILS):
        raise HTTPException(403, "Only the Topolearn team can do this.")
    return ident


# ------------------------------------------------------- deleting accounts


def delete_firebase_account(uid: str) -> bool:
    """Remove the person's Firebase sign-in record too, so deleting the
    account leaves nothing behind. Uses the server's own Google credentials
    (on Cloud Run: its service account, which needs the "Firebase
    Authentication Admin" role). Best effort: logs and returns False on failure."""
    project = config.FIREBASE_PROJECT_ID
    if not project:
        return False
    try:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        res = AuthorizedSession(creds).post(
            f"https://identitytoolkit.googleapis.com/v1/projects/{project}/accounts:delete",
            json={"localId": uid},
            timeout=10,
        )
        if res.ok or "USER_NOT_FOUND" in res.text:
            return True
        log.warning("deleting Firebase user %s failed: %s %s", uid, res.status_code, res.text[:300])
    except Exception:
        log.warning("deleting Firebase user %s failed", uid, exc_info=True)
    return False
