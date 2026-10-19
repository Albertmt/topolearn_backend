"""Email/password sign-up support: the verification and password-reset emails.

Accounts themselves live in Firebase Authentication (the web app creates
them with the Firebase SDK). What Firebase can't do for us is send the
emails from our own address, so the server asks Firebase for the action
link (Identity Toolkit `accounts:sendOobCode` with `returnOobLink`) and mails
it through Resend from do-not-reply@topolearnapp.com.

The verification mail carries a *sign-in* link: opening it signs the person
in on the web and marks the address verified, so a brand-new account goes
straight into onboarding. Their password keeps working afterwards.
"""
import logging
import re
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Literal, Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import auth, config, mail

log = logging.getLogger("topolearn.emailauth")
router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------------------------------------------------------------- rate limit

class _Limiter:
    """At most `burst` sends per key per `window` seconds. In-memory, per instance:
    enough to stop someone hammering the endpoint from the browser."""

    def __init__(self, burst: int, window: float) -> None:
        self.burst, self.window = burst, window
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            q = self._hits[key]
            while q and q[0] < now - self.window:
                q.popleft()
            if len(q) >= self.burst:
                return False
            q.append(now)
            return True


_per_email = _Limiter(burst=4, window=3600)
_per_ip = _Limiter(burst=20, window=3600)


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else None) or (request.client.host if request.client else "?")


# ------------------------------------------------------------- Firebase links

def _oob_link(request_type: Literal["EMAIL_SIGNIN", "PASSWORD_RESET", "VERIFY_EMAIL"], email: str, continue_url: str) -> Optional[str]:
    """Ask Firebase for an action link without it sending the email itself.
    Uses the server's Google credentials (Cloud Run service account)."""
    project = config.FIREBASE_PROJECT_ID
    if not project:
        return None
    try:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        res = AuthorizedSession(creds).post(
            f"https://identitytoolkit.googleapis.com/v1/projects/{project}/accounts:sendOobCode",
            json={
                "requestType": request_type,
                "email": email,
                "returnOobLink": True,
                "continueUrl": continue_url,
                "canHandleCodeInApp": request_type == "EMAIL_SIGNIN",
            },
            timeout=10,
        )
        if res.ok:
            return res.json().get("oobLink")
        log.warning("sendOobCode %s for %s failed: %s %s", request_type, email, res.status_code, res.text[:300])
    except Exception:
        log.warning("sendOobCode %s for %s failed", request_type, email, exc_info=True)
    return None


# ----------------------------------------------------------------- endpoints

@router.post("/verification-email", status_code=202)
def send_verification_email(request: Request):
    """Email the signed-in (not yet verified) person a link that signs them in
    and verifies their address. Needs their Firebase ID token."""
    ident = auth.identity(request)
    if not ident.email:
        raise HTTPException(400, "This account has no email address")
    if ident.email_verified:
        return Response(status_code=204)
    if not _per_email.allow(ident.email) or not _per_ip.allow(_client_ip(request)):
        raise HTTPException(429, "Too many emails sent. Check your inbox, or try again in an hour.")
    link = _oob_link("EMAIL_SIGNIN", ident.email, f"{config.SITE_URL}/verify?email={quote(ident.email)}")
    if not link:
        raise HTTPException(503, "Couldn't create a verification link right now. Try again in a minute.")
    sent = mail.send(
        ident.email,
        "Confirm your email for Topolearn",
        "Confirm your email",
        "One click and you're in. This link signs you in to Topolearn and confirms your email address. It expires in an hour.",
        "Confirm and sign in",
        link,
        "If you didn't create a Topolearn account, you can ignore this email and nothing will happen.",
    )
    if not sent and config.RESEND_API_KEY:
        raise HTTPException(503, "Couldn't send the email right now. Try again in a minute.")
    return Response(status_code=202)


class ResetBody(BaseModel):
    email: str = Field(..., max_length=254)


@router.post("/password-reset", status_code=202)
def send_password_reset(body: ResetBody, request: Request):
    """Email a password-reset link. Always answers 202 so the form can't be
    used to find out which addresses have accounts."""
    email = body.email.strip().lower()
    if not _EMAIL.match(email):
        raise HTTPException(400, "That doesn't look like an email address")
    if not _per_email.allow(email) or not _per_ip.allow(_client_ip(request)):
        raise HTTPException(429, "Too many requests. Try again in an hour.")
    link = _oob_link("PASSWORD_RESET", email, f"{config.SITE_URL}/?signin=1")
    if link:  # None also when there is no such account; say nothing either way
        mail.send(
            email,
            "Reset your Topolearn password",
            "Reset your password",
            "Someone asked to reset the password for this Topolearn account. If that was you, pick a new password with the button below. The link expires in an hour.",
            "Choose a new password",
            link,
            "If you didn't ask for this, ignore this email; your password stays as it is.",
        )
    return Response(status_code=202)
