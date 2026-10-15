from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# root -> a -> c ;  root -> b -> c ;  a -> d
FIXTURE = {
    "concepts": [{"id": i, "name": i.upper()} for i in ["root", "a", "b", "c", "d"]],
    "prerequisites": [
        {"concept": "root", "requires": "a"},
        {"concept": "root", "requires": "b"},
        {"concept": "a", "requires": "c"},
        {"concept": "b", "requires": "c"},
        {"concept": "a", "requires": "d"},
    ],
}


@pytest.fixture()
def client(make_client):
    return make_client(FIXTURE)


def _user(client, name="Ada"):
    return client.post("/api/v1/users", json={"name": name}).json()["id"]


def _ids(g):
    return {n["id"] for n in g["nodes"]}


def test_progress_roundtrip_and_graph_annotations(client):
    uid = _user(client)
    r = client.put(f"/api/v1/users/{uid}/progress/a", json={"understanding": 70})
    assert r.status_code == 200
    assert r.json() == {"concept_id": "a", "name": "A", "understanding": 70, "status": None, "newly_ready": []}
    # partial update keeps the other field
    r = client.put(f"/api/v1/users/{uid}/progress/a", json={"status": "need_to_learn"}).json()
    assert r["understanding"] == 70 and r["status"] == "need_to_learn"

    g = client.get("/api/v1/concepts/root/graph", params={"user_id": uid}).json()
    a = next(n for n in g["nodes"] if n["id"] == "a")
    assert a["understanding"] == 70 and a["status"] == "need_to_learn"
    assert next(n for n in g["nodes"] if n["id"] == "b")["understanding"] is None

    # clearing both fields removes the row
    client.put(f"/api/v1/users/{uid}/progress/a", json={"understanding": None, "status": None})
    assert client.get(f"/api/v1/users/{uid}/progress").json() == []

    assert client.put(f"/api/v1/users/{uid}/progress/a", json={"understanding": 101}).status_code == 422
    assert client.put(f"/api/v1/users/{uid}/progress/a", json={"status": "bogus"}).status_code == 422
    assert client.put(f"/api/v1/users/{uid}/progress/zzz", json={"understanding": 1}).status_code == 404
    assert client.put("/api/v1/users/nobody/progress/a", json={"understanding": 1}).status_code == 404


def test_hidden_concepts_are_pruned_per_user(client):
    uid, other = _user(client, "Ada"), _user(client, "Bob")
    client.put(f"/api/v1/users/{uid}/progress/a", json={"status": "hidden"})

    g = client.get("/api/v1/concepts/root/graph", params={"user_id": uid}).json()
    # a and its exclusive prerequisite d are gone; c stays because b still needs it
    assert _ids(g) == {"root", "b", "c"}
    assert all("a" not in (e["concept"], e["requires"]) for e in g["edges"])
    root = next(n for n in g["nodes"] if n["id"] == "root")
    assert root["hidden_prereqs"] == 0  # a user-hidden prerequisite isn't "more to load"

    # other users and anonymous requests are unaffected
    assert _ids(client.get("/api/v1/concepts/root/graph", params={"user_id": other}).json()) == {"root", "a", "b", "c", "d"}
    assert _ids(client.get("/api/v1/concepts/root/graph").json()) == {"root", "a", "b", "c", "d"}

    # opening a hidden concept directly still shows it as the root
    assert "a" in _ids(client.get("/api/v1/concepts/a/graph", params={"user_id": uid}).json())

    hidden = client.get(f"/api/v1/users/{uid}/progress", params={"status": "hidden"}).json()
    assert [h["concept_id"] for h in hidden] == ["a"]
    assert client.delete(f"/api/v1/users/{uid}/progress/a").status_code == 204
    assert _ids(client.get("/api/v1/concepts/root/graph", params={"user_id": uid}).json()) == {"root", "a", "b", "c", "d"}


def test_sources_and_votes(client):
    ada, bob = _user(client, "Ada"), _user(client, "Bob")
    r = client.post("/api/v1/concepts/a/sources",
                    json={"user_id": ada, "url": "www.example.com/learn-a", "kind": "video"})
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["url"] == "https://www.example.com/learn-a"
    assert s["title"] == "example.com"
    assert s["score"] == 1 and s["my_vote"] == 1 and s["added_by_me"] and s["added_by_name"] == "Ada"

    # duplicates and junk are rejected
    assert client.post("/api/v1/concepts/a/sources",
                       json={"user_id": bob, "url": "https://www.example.com/learn-a"}).status_code == 409
    assert client.post("/api/v1/concepts/a/sources", json={"user_id": bob, "url": "not a link"}).status_code == 422
    assert client.post("/api/v1/concepts/a/sources",
                       json={"user_id": bob, "url": "https://x.org", "kind": "podcast"}).status_code == 422

    s2 = client.post("/api/v1/concepts/a/sources",
                     json={"user_id": bob, "url": "https://b.org/a", "title": "Bob's notes"}).json()
    # Bob downvotes Ada's, Ada upvotes Bob's twice (idempotent) → Bob's ranks first
    assert client.put(f"/api/v1/sources/{s['id']}/vote", json={"user_id": bob, "value": -1}).json()["score"] == 0
    client.put(f"/api/v1/sources/{s2['id']}/vote", json={"user_id": ada, "value": 1})
    v = client.put(f"/api/v1/sources/{s2['id']}/vote", json={"user_id": ada, "value": 1}).json()
    assert v["score"] == 2 and v["upvotes"] == 2 and v["my_vote"] == 1

    listed = client.get("/api/v1/concepts/a/sources", params={"user_id": bob}).json()
    assert [x["title"] for x in listed] == ["Bob's notes", "example.com"]
    assert listed[1]["my_vote"] == -1 and listed[1]["downvotes"] == 1

    # un-vote
    assert client.put(f"/api/v1/sources/{s['id']}/vote", json={"user_id": bob, "value": 0}).json()["score"] == 1

    # only the author can delete
    assert client.delete(f"/api/v1/sources/{s['id']}", params={"user_id": bob}).status_code == 403
    assert client.delete(f"/api/v1/sources/{s['id']}", params={"user_id": ada}).status_code == 204
    assert [x["id"] for x in client.get("/api/v1/concepts/a/sources").json()] == [s2["id"]]


def test_deleting_user_cleans_up(client):
    ada, bob = _user(client, "Ada"), _user(client, "Bob")
    client.put(f"/api/v1/users/{ada}/progress/a", json={"understanding": 50})
    sid = client.post("/api/v1/concepts/a/sources", json={"user_id": ada, "url": "https://a.org"}).json()["id"]
    client.put(f"/api/v1/sources/{sid}/vote", json={"user_id": bob, "value": 1})

    assert client.delete(f"/api/v1/users/{ada}").status_code == 204
    src = client.get("/api/v1/concepts/a/sources").json()[0]
    assert src["added_by_name"] is None   # source stays for everyone else
    assert src["score"] == 1              # Ada's vote removed, Bob's kept
