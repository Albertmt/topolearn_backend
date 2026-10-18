"""Shared fixtures.

The tests need a Postgres database they may wipe: set TOPOLEARN_TEST_DATABASE_URL,
e.g. a local Postgres (`brew install postgresql@16`) or a scratch Neon branch:

    TOPOLEARN_TEST_DATABASE_URL=postgresql://localhost/topolearn_test pytest

Every test starts with empty user tables; tests that need concepts load their
own graph (make_client), which replaces whatever graph was there.
Never point this at production data.

Sign-in: real Firebase tokens are replaced by fake ones (`fake_token`) that
`auth.verify_token` accepts in tests; everything after the signature check
runs for real. `sign_in`/`new_user` create accounts through
`POST /api/v1/session`, and the clients from `make_client`/`AuthedClient`
attach the right learner's token whenever a request names a `user_id`.
test_auth.py checks real token verification separately.
"""
import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional

import psycopg
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config as _app_config  # noqa: E402  (importing it reads .env)

TEST_URL = os.environ.get("TOPOLEARN_TEST_DATABASE_URL", "").strip()


def pytest_configure(config):
    if not TEST_URL:
        pytest.exit(
            "Set TOPOLEARN_TEST_DATABASE_URL to a Postgres database the tests may wipe, e.g.\n"
            "  TOPOLEARN_TEST_DATABASE_URL=postgresql://localhost/topolearn_test pytest",
            returncode=2,
        )
    prod = os.environ.get("DATABASE_URL", "").strip()  # includes .env
    if prod and prod == TEST_URL:
        pytest.exit("TOPOLEARN_TEST_DATABASE_URL is the same as DATABASE_URL; refusing to wipe it.", returncode=2)
    _app_config.DATABASE_URL = TEST_URL  # the app under test only ever sees the test database


# ------------------------------------------------------------------ sign-in

ADMIN_EMAIL = "admin@topolearnapp.com"

# Our user id -> the fake token that signs in as them.
TOKENS: Dict[str, str] = {}
_counter = [0]


def fake_token(uid: str, name: Optional[str] = None, email: Optional[str] = None,
               email_verified: bool = True, provider: str = "google.com") -> str:
    claims = {"sub": uid, "name": name, "email": email, "email_verified": email_verified,
              "provider": provider}
    return "test." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode()


def _fake_verify(token: str):
    from app import auth

    if not token.startswith("test."):
        raise auth._unauthorized(auth.INVALID)
    c = json.loads(base64.urlsafe_b64decode(token[5:]))
    email = c.get("email")
    return auth.Identity(uid=c["sub"], name=c.get("name"), email=email.lower() if email else None,
                         email_verified=c.get("email_verified", False), provider=c.get("provider"))


def bearer(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def admin_headers() -> Dict[str, str]:
    return bearer(fake_token("admin-uid", "Admin", ADMIN_EMAIL))


@pytest.fixture(autouse=True)
def fake_firebase(monkeypatch):
    """Accept fake tokens instead of real Firebase ones; never call Google."""
    from app import auth, config

    monkeypatch.setattr(auth, "verify_token", _fake_verify)
    monkeypatch.setattr(config, "ADMIN_EMAILS", {ADMIN_EMAIL})
    deleted: List[str] = []
    monkeypatch.setattr(auth, "delete_firebase_account", lambda uid: deleted.append(uid) or True)
    TOKENS.clear()
    auth.forget()
    yield deleted


class AuthedClient(TestClient):
    """A TestClient that signs requests in as the learner they name: a
    `user_id` in the path (/users/<id>/...), the query string or a JSON body.
    Requests that pass their own Authorization header are left alone."""

    def request(self, method, url, *args, **kwargs):
        headers = dict(kwargs.get("headers") or {})
        if not any(k.lower() == "authorization" for k in headers):
            uid = None
            m = re.search(r"/users/([^/?]+)", str(url))
            if m:
                uid = m.group(1)
            params = kwargs.get("params")
            if uid is None and isinstance(params, dict):
                uid = params.get("user_id")
            if uid is None and "user_id=" in str(url):
                uid = re.search(r"user_id=([^&]+)", str(url)).group(1)
            body = kwargs.get("json")
            if uid is None and isinstance(body, dict):
                uid = body.get("user_id")
            if uid in TOKENS:
                headers.update(bearer(TOKENS[uid]))
                kwargs["headers"] = headers
        return super().request(method, url, *args, **kwargs)


def sign_in(client: TestClient, name: Optional[str] = "Ada", email: Optional[str] = None,
            uid: Optional[str] = None, **claims) -> dict:
    """First sign-in creates the account; returns the user."""
    if uid is None:
        _counter[0] += 1
        uid = f"firebase-{_counter[0]}"
    token = fake_token(uid, name, email, **claims)
    r = client.post("/api/v1/session", headers=bearer(token))
    assert r.status_code == 200, r.text
    user = r.json()
    TOKENS[user["id"]] = token
    return user


def lesson(tool: str, name: str, level: int = 1, requires=(), **extra) -> dict:
    """A lesson of `tool` called `name` (id "<tool>.<name>"). `requires` may
    name lessons of the same tool ("b") or of another tool ("python.b")."""
    return {
        "id": f"{tool}.{name}",
        "name": extra.pop("title", name.replace("-", " ").title()),
        "module": extra.pop("module", "basics"),
        "level": level,
        "minutes": extra.pop("minutes", 10),
        "summary": extra.pop("summary", f"About {name}."),
        "why": "Because.",
        "body": extra.pop("body", f"All about {name}."),
        "code": extra.pop("code", f"print('{name}')"),
        "code_lang": "python",
        "takeaways": ["one", "two"],
        "quiz": extra.pop("quiz", [{"q": f"What is {name}?", "options": ["it", "not it"], "answer": 0,
                                    "explain": "It is."}]),
        "requires": [r if "." in r else f"{tool}.{r}" for r in requires],
        **extra,
    }


def tool_doc(tool: str = "t", lessons=None, edges=(), levels=None, requires=(), **fields) -> dict:
    """A tool document as in data/tools/*.json.

    tool_doc("t", ["a", "b", "c"], edges=["a>b", "b>c"], levels={"c": 2})
    -> lessons t.a, t.b, t.c where a requires b and b requires c (so c is the
    foundation). Edges may point at other tools: "a>python.b"."""
    levels = levels or {}
    names = list(lessons or [])
    reqs: Dict[str, List[str]] = {n: [] for n in names}
    for e in edges:
        a, b = e.split(">")
        reqs[a].append(b)
    return {
        "tool": {
            "id": tool,
            "name": fields.pop("name", tool.upper()),
            "category": fields.pop("category", "language"),
            "tagline": fields.pop("tagline", f"The {tool} tool"),
            "description": fields.pop("description", f"{tool} is a tool."),
            "icon": fields.pop("icon", tool[:2]),
            "color": fields.pop("color", "#336699"),
            "official_docs": "https://example.com/docs",
            "aliases": fields.pop("aliases", []),
            "requires": [{"tool": r.split(":")[0], "level": int(r.split(":")[1]) if ":" in r else 1}
                         for r in requires],
            **fields,
        },
        "modules": [{"id": "basics", "name": "Basics"}, {"id": "more", "name": "More"}],
        "concepts": [lesson(tool, n, levels.get(n, 1), reqs[n]) for n in names],
    }


def wipe_users() -> None:
    from app import userdb

    with psycopg.connect(TEST_URL, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS " + ", ".join(userdb.TABLES) + " CASCADE")
    userdb.forget_schema()
    from app import auth

    auth.forget()


def drop_graph() -> None:
    with psycopg.connect(TEST_URL, autocommit=True) as conn:
        for schema in ("graph", "graph_build", "graph_old"):
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def load_graph(data) -> None:
    from scripts.build_db import load_into

    load_into(TEST_URL, data)


def run_loader(data: Dict, tmp_path: Path) -> subprocess.CompletedProcess:
    """Run scripts/build_db.py as a command, as a person would."""
    src = tmp_path / "src.json"
    src.write_text(json.dumps(data))
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_db.py"), str(src), "--database-url", TEST_URL],
        capture_output=True, text=True,
    )


@pytest.fixture(autouse=True)
def clean_users():
    """Every test starts without users, progress, sources, reports, ..."""
    wipe_users()
    yield


@pytest.fixture()
def make_client() -> Callable[..., TestClient]:
    """make_client(tool_doc(...) or [tool_doc(...), ...]) -> TestClient with
    that catalog loaded. User data is kept across calls within a test (like
    reloading the catalog in production)."""

    def _make(data) -> TestClient:
        load_graph(data)
        from app.main import app

        return AuthedClient(app)

    return _make


def new_user(client: TestClient, name: str = "Ada") -> str:
    return sign_in(client, name)["id"]
