"""Shared fixtures.

The tests need a Postgres database they may wipe: set TOPOLEARN_TEST_DATABASE_URL,
e.g. a local Postgres (`brew install postgresql@16`) or a scratch Neon branch:

    TOPOLEARN_TEST_DATABASE_URL=postgresql://localhost/topolearn_test pytest

Every test starts with empty user tables; tests that need concepts load their
own graph (make_client), which replaces whatever graph was there.
Never point this at production data.
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List

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


def concepts(*ids: str, **extra) -> List[dict]:
    return [{"id": i, "name": i.replace("_", " ").title(), **extra} for i in ids]


def edges(*pairs: str) -> List[dict]:
    """edges("a>b", "b>c") — a requires b, b requires c."""
    out = []
    for p in pairs:
        a, b = p.split(">")
        out.append({"concept": a, "requires": b})
    return out


def wipe_users() -> None:
    from app import userdb

    with psycopg.connect(TEST_URL, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS " + ", ".join(userdb.TABLES) + " CASCADE")
    userdb.forget_schema()


def drop_graph() -> None:
    with psycopg.connect(TEST_URL, autocommit=True) as conn:
        for schema in ("graph", "graph_build", "graph_old"):
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def load_graph(data: Dict) -> None:
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
    """make_client({"concepts": [...], "prerequisites": [...]}) -> TestClient
    with that concept graph loaded. User data is kept across calls within a
    test (like reloading the concept data in production)."""

    def _make(data: Dict, users_db=None) -> TestClient:  # users_db: kept for old call sites
        load_graph(data)
        from app.main import app

        return TestClient(app)

    return _make


def new_user(client: TestClient, name: str = "Ada") -> str:
    r = client.post("/api/v1/users", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]
