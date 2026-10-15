from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent

FIXTURE = {
    "concepts": [
        {"id": "numbers", "name": "Numbers"},
        {"id": "addition", "name": "Addition"},
        {"id": "multiplication", "name": "Multiplication", "aliases": ["times"]},
        {"id": "algebra", "name": "Algebra"},
        {"id": "linear_algebra", "name": "Linear Algebra", "difficulty": 0.4},
        {"id": "matrix", "name": "Matrix"},
    ],
    "prerequisites": [
        {"concept": "addition", "requires": "numbers"},
        {"concept": "multiplication", "requires": "addition"},
        {"concept": "algebra", "requires": "multiplication"},
        {"concept": "algebra", "requires": "addition"},
        {"concept": "matrix", "requires": "algebra"},
        {"concept": "linear_algebra", "requires": "matrix"},
        {"concept": "linear_algebra", "requires": "algebra"},
    ],
}


@pytest.fixture(scope="module")
def client():
    from conftest import load_graph

    load_graph(FIXTURE)
    from app.main import app

    return TestClient(app)


@pytest.fixture(scope="module")
def monkeypatch_module():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["concepts"] == 6


def test_search_prefix_and_exact_first(client):
    r = client.get("/api/v1/search", params={"q": "alg"}).json()
    names = [c["name"] for c in r["results"]]
    assert names[0] == "Algebra"
    assert "Linear Algebra" in names

    r = client.get("/api/v1/search", params={"q": "linear algebra"}).json()
    assert r["results"][0]["id"] == "linear_algebra"


def test_search_alias(client):
    r = client.get("/api/v1/search", params={"q": "times"}).json()
    assert r["results"][0]["id"] == "multiplication"


def test_search_weird_input_does_not_crash(client):
    for q in ['"', "a*b", "(", "NEAR", "%", "_"]:
        assert client.get("/api/v1/search", params={"q": q}).status_code == 200


def test_graph_full(client):
    g = client.get("/api/v1/concepts/linear_algebra/graph").json()
    ids = {n["id"] for n in g["nodes"]}
    assert ids == {"linear_algebra", "matrix", "algebra", "multiplication", "addition", "numbers"}
    edges = {(e["concept"], e["requires"]) for e in g["edges"]}
    assert ("linear_algebra", "algebra") in edges
    assert ("algebra", "addition") in edges
    assert len(edges) == 7
    assert g["truncated"] is False
    depth = {n["id"]: n["depth"] for n in g["nodes"]}
    assert depth["linear_algebra"] == 0 and depth["algebra"] == 1


def test_graph_depth_limit_marks_hidden(client):
    g = client.get("/api/v1/concepts/linear_algebra/graph", params={"depth": 1}).json()
    ids = {n["id"] for n in g["nodes"]}
    assert ids == {"linear_algebra", "matrix", "algebra"}
    assert g["truncated"] is True
    hidden = {n["id"]: n["hidden_prereqs"] for n in g["nodes"]}
    assert hidden["algebra"] == 2


def test_graph_node_limit(client):
    g = client.get("/api/v1/concepts/linear_algebra/graph", params={"max_nodes": 2}).json()
    assert len(g["nodes"]) == 2
    assert g["truncated"] is True
    ids = {n["id"] for n in g["nodes"]}
    for e in g["edges"]:
        assert e["concept"] in ids and e["requires"] in ids


def test_unknown_concept(client):
    assert client.get("/api/v1/concepts/nope").status_code == 404
    assert client.get("/api/v1/concepts/nope/graph").status_code == 404


def test_unlocks(client):
    r = client.get("/api/v1/concepts/algebra/unlocks").json()
    assert {c["id"] for c in r["results"]} == {"matrix", "linear_algebra"}


def test_cycle_rejected(tmp_path):
    bad = {
        "concepts": [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}],
        "prerequisites": [{"concept": "a", "requires": "b"}, {"concept": "b", "requires": "a"}],
    }
    from conftest import run_loader

    res = run_loader(bad, tmp_path)
    assert res.returncode != 0
    assert "cycle" in res.stderr.lower()
