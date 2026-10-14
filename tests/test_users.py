import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent

FIXTURE = {
    "concepts": [
        {"id": "machine_learning", "name": "Machine Learning", "type": "field"},
        {"id": "statistics", "name": "Statistics", "type": "field"},
        {"id": "linear_regression", "name": "Linear Regression", "type": "algorithm"},
        {"id": "machine_learning_ops", "name": "Machine Learning Operations", "type": "concept"},
    ],
    "prerequisites": [{"concept": "linear_regression", "requires": "statistics"}],
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    src, db = tmp_path / "src.json", tmp_path / "t.db"
    src.write_text(json.dumps(FIXTURE))
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_db.py"), str(src), "--out", str(db)], check=True)
    from app import config

    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(config, "USERS_DB_PATH", tmp_path / "users.db")
    from app.main import app

    return TestClient(app)


def test_user_lifecycle(client):
    assert client.get("/api/v1/users").json() == []

    r = client.post("/api/v1/users", json={"name": "  Ada   Lovelace ", "email": "ADA@example.com"})
    assert r.status_code == 201
    u = r.json()
    assert u["name"] == "Ada Lovelace" and u["email"] == "ada@example.com"
    assert u["onboarded"] is False and u["interests"] == []

    r = client.patch(f"/api/v1/users/{u['id']}", json={
        "age": 30, "education_level": "masters",
        "interests": ["machine_learning", "statistics", "machine_learning"], "onboarded": True,
    })
    assert r.status_code == 200, r.text
    u2 = r.json()
    assert u2["age"] == 30 and u2["education_level"] == "masters" and u2["onboarded"] is True
    assert [i["id"] for i in u2["interests"]] == ["machine_learning", "statistics"]
    assert u2["interests"][0]["name"] == "Machine Learning"

    assert [x["id"] for x in client.get("/api/v1/users").json()] == [u["id"]]
    assert client.get(f"/api/v1/users/{u['id']}").json()["age"] == 30

    assert client.delete(f"/api/v1/users/{u['id']}").status_code == 204
    assert client.get(f"/api/v1/users/{u['id']}").status_code == 404


def test_validation(client):
    assert client.post("/api/v1/users", json={"name": "   "}).status_code == 422
    assert client.post("/api/v1/users", json={"name": "X", "email": "nope"}).status_code == 422
    assert client.post("/api/v1/users", json={"name": "X", "email": ""}).json()["email"] is None
    uid = client.post("/api/v1/users", json={"name": "A", "email": "a@b.co"}).json()["id"]
    assert client.post("/api/v1/users", json={"name": "B", "email": "A@b.co"}).status_code == 409
    assert client.patch(f"/api/v1/users/{uid}", json={"education_level": "wizard"}).status_code == 422
    assert client.patch(f"/api/v1/users/{uid}", json={"interests": ["nope"]}).status_code == 422
    assert client.patch(f"/api/v1/users/{uid}", json={"age": 1}).status_code == 422
    assert client.patch("/api/v1/users/missing", json={"name": "Z"}).status_code == 404
    assert client.delete("/api/v1/users/missing").status_code == 404


def test_disciplines(client):
    picks = client.get("/api/v1/disciplines").json()
    assert {p["id"] for p in picks} == {"machine_learning", "statistics"}  # only those that exist
    found = client.get("/api/v1/disciplines", params={"q": "machine"}).json()
    assert found[0]["id"] == "machine_learning" and found[0]["type"] == "field"
    assert "machine_learning_ops" in [f["id"] for f in found]
