"""Shapes and inputs the app has to survive: single nodes, very wide and very
tall graphs, huge graphs, sparse data, odd text, stale ids, missing DB."""
import random
import time

import pytest
from fastapi.testclient import TestClient

from conftest import concepts, edges, new_user


def _ids(g):
    return {n["id"] for n in g["nodes"]}


def _check_graph_consistency(g):
    ids = _ids(g)
    assert len(ids) == len(g["nodes"]), "duplicate nodes"
    pairs = [(e["concept"], e["requires"]) for e in g["edges"]]
    assert len(pairs) == len(set(pairs)), "duplicate edges"
    for a, b in pairs:
        assert a in ids and b in ids, f"dangling edge {a}->{b}"
    assert g["root"] in ids


def _check_path_order(p):
    pos = {s["id"]: i for i, s in enumerate(p["steps"])}
    for s in p["steps"]:
        for r in s["requires"]:
            assert pos[r] < pos[s["id"]], f"{r} must come before {s['id']}"
    assert p["steps"][-1]["id"] == p["root"]


# ------------------------------------------------------------ single node


def test_single_node_everything_works(make_client):
    c = make_client({"concepts": concepts("alone"), "prerequisites": []})
    uid = new_user(c)
    g = c.get("/api/v1/concepts/alone/graph", params={"user_id": uid}).json()
    assert [n["id"] for n in g["nodes"]] == ["alone"] and g["edges"] == [] and g["truncated"] is False
    p = c.get("/api/v1/concepts/alone/path", params={"user_id": uid}).json()
    assert [s["id"] for s in p["steps"]] == ["alone"] and p["steps"][0]["ready"] is True
    assert c.get("/api/v1/concepts/alone/unlocks").json()["results"] == []
    r = c.put(f"/api/v1/users/{uid}/progress/alone", json={"understanding": 100}).json()
    assert r["newly_ready"] == []
    assert c.put(f"/api/v1/users/{uid}/goals/alone").json()["next"] == []  # goal already reached


# -------------------------------------------------------------- very wide


def test_wide_graph_respects_node_limit(make_client):
    kids = [f"k{i:03d}" for i in range(300)]
    c = make_client({"concepts": concepts("hub", *kids), "prerequisites": edges(*[f"hub>{k}" for k in kids])})
    g = c.get("/api/v1/concepts/hub/graph", params={"max_nodes": 50}).json()
    _check_graph_consistency(g)
    assert len(g["nodes"]) == 50 and g["truncated"] is True
    hub = next(n for n in g["nodes"] if n["id"] == "hub")
    assert hub["hidden_prereqs"] == 300 - 49
    full = c.get("/api/v1/concepts/hub/graph", params={"max_nodes": 500}).json()
    assert len(full["nodes"]) == 301 and full["truncated"] is False
    p = c.get("/api/v1/concepts/hub/path").json()
    assert len(p["steps"]) == 301
    _check_path_order(p)


def test_wide_unlocks_and_newly_ready_are_capped(make_client):
    users_of = [f"u{i:03d}" for i in range(250)]
    c = make_client({"concepts": concepts("base", *users_of),
                     "prerequisites": edges(*[f"{u}>base" for u in users_of])})
    uid = new_user(c)
    assert len(c.get("/api/v1/concepts/base/unlocks", params={"limit": 100}).json()["results"]) == 100
    r = c.put(f"/api/v1/users/{uid}/progress/base", json={"understanding": 100}).json()
    assert len(r["newly_ready"]) == 5


# -------------------------------------------------------------- very tall


def test_tall_chain_depth_limit_and_full_path(make_client):
    n = 80
    ids = [f"c{i:02d}" for i in range(n)]
    c = make_client({"concepts": concepts(*ids),
                     "prerequisites": edges(*[f"{ids[i]}>{ids[i + 1]}" for i in range(n - 1)])})
    g = c.get("/api/v1/concepts/c00/graph", params={"depth": 10}).json()
    _check_graph_consistency(g)
    assert len(g["nodes"]) == 11 and g["truncated"] is True
    last = next(x for x in g["nodes"] if x["id"] == "c10")
    assert last["hidden_prereqs"] == 1 and last["depth"] == 10
    # max allowed depth is 30
    assert c.get("/api/v1/concepts/c00/graph", params={"depth": 31}).status_code == 422
    p = c.get("/api/v1/concepts/c00/path").json()
    assert [s["id"] for s in p["steps"]] == list(reversed(ids))  # deepest first
    _check_path_order(p)


# -------------------------------------------------------------------- huge


def test_huge_random_dag_is_bounded_and_fast(make_client):
    rnd = random.Random(7)
    n = 4000
    ids = [f"n{i}" for i in range(n)]
    pairs = set()
    for i in range(n - 1):
        for _ in range(rnd.randint(1, 4)):
            j = rnd.randint(i + 1, min(n - 1, i + 60))  # only point "deeper" => acyclic
            pairs.add(f"{ids[i]}>{ids[j]}")
    data = {"concepts": [{"id": i, "name": f"Node {i}", "difficulty": rnd.random()} for i in ids],
            "prerequisites": edges(*sorted(pairs))}
    c = make_client(data)
    uid = new_user(c)
    for x in ids[5:40]:
        c.put(f"/api/v1/users/{uid}/progress/{x}", json={"status": "hidden"})

    t = time.perf_counter()
    g = c.get("/api/v1/concepts/n0/graph", params={"user_id": uid, "max_nodes": 500, "depth": 30}).json()
    assert time.perf_counter() - t < 2.0
    _check_graph_consistency(g)
    assert len(g["nodes"]) <= 500 and g["truncated"] is True
    assert not (_ids(g) & set(ids[5:40]))

    t = time.perf_counter()
    p = c.get("/api/v1/concepts/n0/path", params={"user_id": uid}).json()
    assert time.perf_counter() - t < 5.0
    assert p["truncated"] is True and len(p["steps"]) == 1500
    assert p["steps"][-1]["id"] == "n0"

    s = c.get("/api/v1/search", params={"q": "node n39", "limit": 20}).json()
    assert s["results"] and len(s["results"]) <= 20


# ------------------------------------------------------- diamonds / sharing


def test_shared_prerequisites_appear_once(make_client):
    # every layer fully connected to the next: lots of paths to the same nodes
    layers = [[f"l{d}_{i}" for i in range(4)] for d in range(5)]
    pairs = [f"{a}>{b}" for d in range(4) for a in layers[d] for b in layers[d + 1]]
    pairs += [f"top>{x}" for x in layers[0]]
    c = make_client({"concepts": concepts("top", *[x for l in layers for x in l]), "prerequisites": edges(*pairs)})
    g = c.get("/api/v1/concepts/top/graph").json()
    _check_graph_consistency(g)
    assert len(g["nodes"]) == 21
    p = c.get("/api/v1/concepts/top/path").json()
    assert len(p["steps"]) == 21
    _check_path_order(p)


# ------------------------------------------------------------ missing data


def test_concepts_with_minimal_data(make_client):
    # only id + name: no type, description, difficulty, example, wiki, aliases
    c = make_client({"concepts": [{"id": "bare", "name": "Bare"}, {"id": "bare2", "name": "Bare Two"}],
                     "prerequisites": [{"concept": "bare", "requires": "bare2"}]})
    one = c.get("/api/v1/concepts/bare").json()
    assert one["type"] is None and one["description"] is None and one["difficulty"] is None
    assert one["example"] is None and one["wiki_url"] is None and one["domain"] is None
    g = c.get("/api/v1/concepts/bare/graph").json()
    assert g["edges"][0]["why"] is None
    p = c.get("/api/v1/concepts/bare/path").json()
    assert all(s["minutes"] > 0 for s in p["steps"])  # default difficulty used
    assert c.get("/p/bare").status_code == 200


def test_build_rejects_and_repairs_bad_input(make_client, tmp_path):
    pass


    from conftest import run_loader

    res = run_loader({
        "concepts": [{"id": "a", "name": "A"}, {"id": "", "name": "no id"}, {"id": "b"}],
        "prerequisites": [{"concept": "a", "requires": "a"}, {"concept": "a", "requires": "ghost"}],
    }, tmp_path)
    assert res.returncode == 0, res.stderr
    assert "self-loop" in res.stdout and "unknown concept" in res.stdout and "skipping concept" in res.stdout


# ----------------------------------------------------------- odd text input


@pytest.mark.parametrize("q", ["Gödel", "ν-SVM", "naïve bayes", "  spaced   out  ", "🙂", "a" * 100,
                               "'; DROP TABLE concepts; --", "%", "_", "\"", "*", "NEAR(", "AND OR NOT"])
def test_search_survives_odd_queries(make_client, q):
    c = make_client({"concepts": [{"id": "godel", "name": "Gödel's Incompleteness Theorems"},
                                  {"id": "nu_svm", "name": "ν-SVM"},
                                  {"id": "naive_bayes", "name": "Naïve Bayes", "aliases": ["naive bayes"]}],
                     "prerequisites": []})
    r = c.get("/api/v1/search", params={"q": q})
    assert r.status_code == 200


def test_search_finds_unicode_and_aliases(make_client):
    c = make_client({"concepts": [{"id": "nu_svm", "name": "ν-SVM"},
                                  {"id": "naive_bayes", "name": "Naïve Bayes", "aliases": ["naive bayes"]}],
                     "prerequisites": []})
    assert c.get("/api/v1/search", params={"q": "ν"}).json()["results"][0]["id"] == "nu_svm"
    assert c.get("/api/v1/search", params={"q": "naive bayes"}).json()["results"][0]["id"] == "naive_bayes"
    assert c.get("/api/v1/search", params={"q": ""}).status_code == 422
    assert c.get("/api/v1/search", params={"q": "a" * 101}).status_code == 422
    assert c.get("/api/v1/search", params={"q": "x", "limit": 0}).status_code == 422


@pytest.mark.parametrize("cid", ["nope", "a'; DROP TABLE concepts;--", "%25", "..", "ünï"])
def test_unknown_or_hostile_ids_are_404(make_client, cid):
    c = make_client({"concepts": concepts("a"), "prerequisites": []})
    for path in ("", "/graph", "/path", "/unlocks", "/sources", "/community"):
        assert c.get(f"/api/v1/concepts/{cid}{path}").status_code == 404, path
    assert c.get("/api/v1/concepts/a").status_code == 200  # still intact


def test_unicode_user_and_long_names(make_client):
    c = make_client({"concepts": concepts("a"), "prerequisites": []})
    r = c.post("/api/v1/users", json={"name": "Ալբերտ Թամազյան 🙂", "email": "Albert@Example.COM"})
    assert r.status_code == 201
    assert r.json()["name"] == "Ալբերտ Թամազյան 🙂" and r.json()["email"] == "albert@example.com"
    assert c.post("/api/v1/users", json={"name": "x" * 81}).status_code == 422
    assert c.post("/api/v1/users", json={}).status_code == 422


# -------------------------------------------- stale ids after a data rebuild


def test_user_data_pointing_at_removed_concepts(make_client, tmp_path):
    c = make_client({"concepts": concepts("a", "b", "gone"), "prerequisites": edges("a>b")})
    uid = new_user(c)
    c.patch(f"/api/v1/users/{uid}", json={"interests": ["gone", "a"]})
    c.put(f"/api/v1/users/{uid}/progress/gone", json={"understanding": 100})
    c.put(f"/api/v1/users/{uid}/goals/gone")
    c.put(f"/api/v1/users/{uid}/goals/a")
    c.post("/api/v1/reports", json={"user_id": uid, "kind": "other", "concept_id": "gone", "message": "x"})

    # Rebuild the concept DB without "gone"; user data stays.
    c = make_client({"concepts": concepts("a", "b"), "prerequisites": edges("a>b")})
    user = c.get(f"/api/v1/users/{uid}").json()
    assert [i["id"] for i in user["interests"]] == ["a"]
    assert [g["concept"]["id"] for g in c.get(f"/api/v1/users/{uid}/goals").json()] == ["a"]
    prog = {p["concept_id"]: p for p in c.get(f"/api/v1/users/{uid}/progress").json()}
    assert prog["gone"]["name"] is None
    assert c.get(f"/api/v1/users/{uid}/stats").status_code == 200
    rep = c.get("/api/v1/reports").json()[0]
    assert rep["concept"]["id"] == "gone"  # still readable for review


# ------------------------------------------------------------- hidden root


def test_hidden_root_is_still_shown_when_opened_directly(make_client):
    c = make_client({"concepts": concepts("a", "b"), "prerequisites": edges("a>b")})
    uid = new_user(c)
    c.put(f"/api/v1/users/{uid}/progress/a", json={"status": "hidden"})
    assert "a" in _ids(c.get("/api/v1/concepts/a/graph", params={"user_id": uid}).json())
    assert c.get("/api/v1/concepts/a/path", params={"user_id": uid}).json()["steps"][-1]["id"] == "a"


def test_everything_hidden_leaves_just_the_root(make_client):
    c = make_client({"concepts": concepts("r", "x", "y"), "prerequisites": edges("r>x", "r>y")})
    uid = new_user(c)
    for cid in ("x", "y"):
        c.put(f"/api/v1/users/{uid}/progress/{cid}", json={"status": "hidden"})
    g = c.get("/api/v1/concepts/r/graph", params={"user_id": uid}).json()
    assert _ids(g) == {"r"} and g["edges"] == [] and g["nodes"][0]["hidden_prereqs"] == 0


# ------------------------------------------------------------- unknown user


def test_unknown_user_on_personalised_reads_is_harmless(make_client):
    c = make_client({"concepts": concepts("a", "b"), "prerequisites": edges("a>b")})
    # graph/path with a user that doesn't exist behave like anonymous
    assert len(c.get("/api/v1/concepts/a/graph", params={"user_id": "ghost"}).json()["nodes"]) == 2
    assert len(c.get("/api/v1/concepts/a/path", params={"user_id": "ghost"}).json()["steps"]) == 2
    for method, url in [("get", "/api/v1/users/ghost"), ("get", "/api/v1/users/ghost/progress"),
                        ("get", "/api/v1/users/ghost/goals"), ("get", "/api/v1/users/ghost/stats"),
                        ("delete", "/api/v1/users/ghost/progress/a")]:
        assert getattr(c, method)(url).status_code == 404, url


# -------------------------------------------------------------- missing DB


def test_graph_not_loaded_gives_clear_503():
    from conftest import drop_graph

    from app.main import app

    drop_graph()
    c = TestClient(app)
    r = c.get("/health")
    assert r.status_code == 503 and "build_db.py" in r.json()["detail"]
    r = c.get("/api/v1/search", params={"q": "a"})
    assert r.status_code == 503 and "build_db.py" in r.json()["detail"]
    # User endpoints that don't need concepts still work.
    assert c.post("/api/v1/users", json={"name": "Ada"}).status_code == 201


def test_no_database_configured_gives_clear_503(monkeypatch):
    from app import config, db
    from app.main import app

    monkeypatch.setattr(config, "DATABASE_URL", "")
    db.reset_connections()
    r = TestClient(app).get("/health")
    assert r.status_code == 503 and "DATABASE_URL" in r.json()["detail"]


def test_unreachable_database_gives_503_not_a_crash(monkeypatch):
    from app import config, userdb
    from app.main import app

    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://nobody@127.0.0.1:1/nothing?connect_timeout=2")
    userdb.forget_schema()
    c = TestClient(app)
    assert c.get("/health").status_code == 503
    assert c.post("/api/v1/users", json={"name": "Ada"}).status_code == 503
