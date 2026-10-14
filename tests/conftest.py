"""Shared fixtures: build a throwaway concept DB from a dict and get a client."""
import json
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent


def concepts(*ids: str, **extra) -> List[dict]:
    return [{"id": i, "name": i.replace("_", " ").title(), **extra} for i in ids]


def edges(*pairs: str) -> List[dict]:
    """edges("a>b", "b>c") — a requires b, b requires c."""
    out = []
    for p in pairs:
        a, b = p.split(">")
        out.append({"concept": a, "requires": b})
    return out


@pytest.fixture()
def make_client(tmp_path, monkeypatch) -> Callable[..., TestClient]:
    """make_client({"concepts": [...], "prerequisites": [...]}) -> TestClient
    with a fresh concept DB and a fresh users DB."""

    def _make(data: Dict, users_db: Optional[Path] = None) -> TestClient:
        src, db = tmp_path / "src.json", tmp_path / "concepts.db"
        src.write_text(json.dumps(data))
        res = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "build_db.py"), str(src), "--out", str(db)],
            capture_output=True, text=True,
        )
        assert res.returncode == 0, res.stderr + res.stdout
        from app import config

        monkeypatch.setattr(config, "DB_PATH", db)
        monkeypatch.setattr(config, "USERS_DB_PATH", users_db or tmp_path / "users.db")
        from app.main import app

        return TestClient(app)

    return _make


def new_user(client: TestClient, name: str = "Ada") -> str:
    r = client.post("/api/v1/users", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]
