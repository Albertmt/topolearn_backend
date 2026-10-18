"""Reactions, saved lessons, comments, reports and tool requests."""
import pytest

from conftest import TOKENS, admin_headers, bearer, fake_token, new_user, tool_doc

PY = tool_doc("python", ["basics", "lists"], edges=["lists>basics"], name="Python", aliases=["py"])


@pytest.fixture()
def c(make_client):
    return make_client(PY)


def test_reactions(c):
    ada, bob = new_user(c), new_user(c, "Bob")
    r = c.put("/api/v1/lessons/python.basics/reaction", json={"user_id": ada, "value": 1}).json()
    assert (r["up"], r["down"], r["mine"]) == (1, 0, 1)
    r = c.put("/api/v1/lessons/python.basics/reaction", json={"user_id": bob, "value": -1}).json()
    assert (r["up"], r["down"], r["mine"]) == (1, 1, -1)
    r = c.put("/api/v1/lessons/python.basics/reaction", json={"user_id": ada, "value": -1}).json()
    assert (r["up"], r["down"]) == (0, 2)
    r = c.put("/api/v1/lessons/python.basics/reaction", json={"user_id": ada, "value": 0}).json()
    assert (r["up"], r["down"], r["mine"]) == (0, 1, 0)
    assert c.put("/api/v1/lessons/python.basics/reaction", json={"user_id": ada, "value": 2}).status_code == 422
    assert c.put("/api/v1/lessons/python.nope/reaction", json={"user_id": ada, "value": 1}).status_code == 404
    les = c.get("/api/v1/lessons/python.basics", params={"user_id": bob}).json()
    assert les["social"]["mine"] == -1 and les["social"]["down"] == 1
    # in someone else's name
    r = c.put("/api/v1/lessons/python.basics/reaction", json={"user_id": ada, "value": 1}, headers=bearer(TOKENS[bob]))
    assert r.status_code == 403


def test_saved_lessons(c):
    uid = new_user(c)
    assert c.put(f"/api/v1/users/{uid}/saved/python.lists").status_code == 204
    assert c.put(f"/api/v1/users/{uid}/saved/python.lists").status_code == 204  # idempotent
    c.put(f"/api/v1/users/{uid}/saved/python.basics")
    assert [s["id"] for s in c.get(f"/api/v1/users/{uid}/saved").json()] == ["python.basics", "python.lists"]
    assert c.get("/api/v1/lessons/python.lists", params={"user_id": uid}).json()["social"]["saved"] is True
    assert c.delete(f"/api/v1/users/{uid}/saved/python.lists").status_code == 204
    assert [s["id"] for s in c.get(f"/api/v1/users/{uid}/saved").json()] == ["python.basics"]
    assert c.put(f"/api/v1/users/{uid}/saved/python.nope").status_code == 404


def test_comments_threads_likes_and_sorting(c):
    ada, bob = new_user(c), new_user(c, "Bob")
    first = c.post("/api/v1/lessons/python.basics/comments", json={"user_id": ada, "body": "  Great intro!  "})
    assert first.status_code == 201
    first = first.json()
    assert first["body"] == "Great intro!" and first["author"]["name"] == "Ada" and first["mine"]
    second = c.post("/api/v1/lessons/python.basics/comments", json={"user_id": bob, "body": "Typo in line 2"}).json()
    reply = c.post("/api/v1/lessons/python.basics/comments",
                   json={"user_id": bob, "body": "Agreed", "parent_id": first["id"]}).json()
    # replying to a reply joins the same thread
    deep = c.post("/api/v1/lessons/python.basics/comments",
                  json={"user_id": ada, "body": "Thanks", "parent_id": reply["id"]}).json()
    assert deep["parent_id"] == first["id"]
    liked = c.put(f"/api/v1/comments/{first['id']}/like", json={"user_id": bob, "liked": True}).json()
    assert liked["likes"] == 1 and liked["liked_by_me"]

    top = c.get("/api/v1/lessons/python.basics/comments", params={"user_id": bob}).json()
    assert top["total"] == 4
    assert [x["id"] for x in top["comments"]] == [first["id"], second["id"]]  # most liked first
    assert [x["body"] for x in top["comments"][0]["replies"]] == ["Agreed", "Thanks"]
    assert top["comments"][0]["liked_by_me"] and top["comments"][1]["mine"]
    new = c.get("/api/v1/lessons/python.basics/comments", params={"sort": "new"}).json()
    assert new["comments"][0]["id"] == second["id"]
    assert c.get("/api/v1/lessons/python.basics").json()["social"]["comments"] == 4

    # validation
    assert c.post("/api/v1/lessons/python.basics/comments", json={"user_id": ada, "body": "   "}).status_code == 422
    assert c.post("/api/v1/lessons/python.basics/comments",
                  json={"user_id": ada, "body": "x" * 2001}).status_code == 422
    assert c.post("/api/v1/lessons/python.lists/comments",
                  json={"user_id": ada, "body": "hi", "parent_id": first["id"]}).status_code == 404


def test_only_authors_edit_or_delete(c):
    ada, bob = new_user(c), new_user(c, "Bob")
    cm = c.post("/api/v1/lessons/python.basics/comments", json={"user_id": ada, "body": "v1"}).json()
    c.post("/api/v1/lessons/python.basics/comments", json={"user_id": bob, "body": "reply", "parent_id": cm["id"]})
    assert c.patch(f"/api/v1/comments/{cm['id']}", json={"user_id": bob, "body": "hacked"}).status_code == 403
    r = c.patch(f"/api/v1/comments/{cm['id']}", json={"user_id": ada, "body": "v2"}).json()
    assert r["body"] == "v2" and r["edited_at"]
    assert c.delete(f"/api/v1/comments/{cm['id']}", params={"user_id": bob}).status_code == 403
    assert c.delete(f"/api/v1/comments/{cm['id']}", params={"user_id": ada}).status_code == 204
    assert c.get("/api/v1/lessons/python.basics/comments").json()["total"] == 0  # the reply went with it
    assert c.delete(f"/api/v1/comments/{cm['id']}", params={"user_id": ada}).status_code == 404


def test_deleting_an_account_removes_its_comments_and_reactions(c):
    ada, bob = new_user(c), new_user(c, "Bob")
    c.post("/api/v1/lessons/python.basics/comments", json={"user_id": ada, "body": "hi"})
    c.put("/api/v1/lessons/python.basics/reaction", json={"user_id": ada, "value": 1})
    c.post("/api/v1/lessons/python.basics/comments", json={"user_id": bob, "body": "hello"})
    c.post("/api/v1/tool-requests", json={"name": "Elixir", "user_id": ada})
    assert c.delete(f"/api/v1/users/{ada}").status_code == 204
    s = c.get("/api/v1/lessons/python.basics").json()["social"]
    assert s["comments"] == 1 and s["up"] == 0
    assert c.get("/api/v1/tool-requests/lookup", params={"name": "Elixir"}).json()["votes"] == 0


def test_reports(c):
    uid = new_user(c)
    r = c.post("/api/v1/reports", json={"user_id": uid, "kind": "wrong_content", "lesson_id": "python.basics",
                                        "message": "print is a function in Python 3"})
    assert r.status_code == 201 and r.json()["lesson"]["id"] == "python.basics"
    assert c.post("/api/v1/reports", json={"kind": "typo", "lesson_id": "python.basics"}).status_code == 422
    assert c.post("/api/v1/reports", json={"kind": "missing_prerequisite",
                                           "lesson_id": "python.lists"}).status_code == 422
    r = c.post("/api/v1/reports", json={"kind": "missing_prerequisite", "lesson_id": "python.basics",
                                        "related_lesson_id": "python.lists"})
    assert r.status_code == 201 and r.json()["related_lesson"]["id"] == "python.lists"
    assert c.post("/api/v1/reports", json={"kind": "comment", "lesson_id": "python.basics"}).status_code == 422
    assert c.post("/api/v1/reports", json={"kind": "bogus", "lesson_id": "python.basics",
                                           "message": "x"}).status_code == 422
    assert c.get("/api/v1/reports").status_code == 401
    assert len(c.get("/api/v1/reports", headers=admin_headers()).json()) == 2


def test_tool_requests(c):
    uid = new_user(c)
    r = c.post("/api/v1/tool-requests", json={"name": " Kubernetes ", "user_id": uid}).json()
    assert r == {"name": "Kubernetes", "votes": 1, "requested_by_me": True}
    c.post("/api/v1/tool-requests", json={"name": "kubernetes", "user_id": uid})  # same person again
    r = c.post("/api/v1/tool-requests", json={"name": "KUBERNETES", "anon_id": "abc"}).json()
    assert r["votes"] == 2
    look = c.get("/api/v1/tool-requests/lookup", params={"name": "kubernetes", "user_id": uid}).json()
    assert look["votes"] == 2 and look["requested_by_me"]
    assert c.get("/api/v1/tool-requests/lookup", params={"name": "Elm"}).json()["votes"] == 0
    assert c.post("/api/v1/tool-requests", json={"name": "Python", "anon_id": "x"}).status_code == 409
    assert c.post("/api/v1/tool-requests", json={"name": "Elm"}).status_code == 422
    assert c.get("/api/v1/tool-requests").status_code == 401
    listed = c.get("/api/v1/tool-requests", headers=admin_headers()).json()
    assert listed[0]["name"] == "Kubernetes" and listed[0]["votes"] == 2
    # a signed-in request in someone else's name
    other = bearer(fake_token("someone"))
    assert c.post("/api/v1/tool-requests", json={"name": "Elm", "user_id": uid}, headers=other).status_code == 401
