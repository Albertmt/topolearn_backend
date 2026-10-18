"""A learner's journey: progress, enrolled tools and depth, next lessons,
bulk marking, reviews, feed and stats."""
import time

import pytest

from conftest import TOKENS, bearer, new_user, tool_doc

PY = tool_doc(
    "python", ["basics", "lists", "loops", "comprehensions", "generators", "gil"],
    edges=["lists>basics", "loops>basics", "comprehensions>lists", "comprehensions>loops",
           "generators>comprehensions", "gil>generators"],
    levels={"comprehensions": 2, "generators": 3, "gil": 4}, name="Python",
)
PANDAS = tool_doc(
    "pandas", ["series", "dataframes", "groupby"],
    edges=["series>python.lists", "dataframes>series", "groupby>dataframes"],
    levels={"groupby": 2}, requires=["python:1"], name="pandas", category="library",
)


@pytest.fixture()
def c(make_client):
    return make_client([PY, PANDAS])


def put(c, uid, lid, **body):
    r = c.put(f"/api/v1/users/{uid}/progress/{lid}", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_progress_roundtrip_and_newly_ready(c):
    uid = new_user(c)
    r = put(c, uid, "python.basics", understanding=100)
    assert r["understanding"] == 100
    assert {x["id"] for x in r["newly_ready"]} == {"python.lists", "python.loops"}
    # partial update keeps the other field
    r = put(c, uid, "python.lists", status="need_to_learn")
    r = put(c, uid, "python.lists", understanding=50)
    assert r["status"] == "need_to_learn" and r["understanding"] == 50 and r["newly_ready"] == []
    rows = c.get(f"/api/v1/users/{uid}/progress").json()
    assert rows[0]["lesson"]["id"] == "python.lists"
    assert [x["lesson"]["id"] for x in c.get(f"/api/v1/users/{uid}/progress",
                                             params={"status": "need_to_learn"}).json()] == ["python.lists"]
    assert c.delete(f"/api/v1/users/{uid}/progress/python.lists").status_code == 204
    assert len(c.get(f"/api/v1/users/{uid}/progress").json()) == 1
    assert c.put(f"/api/v1/users/{uid}/progress/python.nope", json={"understanding": 1}).status_code == 404
    assert c.put(f"/api/v1/users/{uid}/progress/python.basics", json={"understanding": 101}).status_code == 422


def test_map_carries_progress_and_next(c):
    uid = new_user(c)
    put(c, uid, "python.basics", understanding=100)
    put(c, uid, "python.loops", status="hidden")
    m = c.get("/api/v1/tools/python/map", params={"user_id": uid, "depth": 2}).json()
    nodes = {n["id"]: n for n in m["nodes"]}
    assert nodes["python.basics"]["understanding"] == 100 and nodes["python.loops"]["status"] == "hidden"
    p = m["progress"]
    assert (p["total"], p["learned"], p["skipped"], p["done"], p["percent"]) == (4, 1, 1, 2, 50)
    assert [n["id"] for n in p["next"]] == ["python.lists"]
    assert p["minutes_left"] == 20 and not p["complete"]


def test_enrol_change_depth_and_list(c):
    uid = new_user(c)
    r = c.put(f"/api/v1/users/{uid}/tools/python", json={"depth": 1})
    assert r.status_code == 200 and r.json()["total"] == 3 and r.json()["depth_name"] == "Essentials"
    assert c.put(f"/api/v1/users/{uid}/tools/python", json={"depth": 4}).json()["total"] == 6
    assert c.put(f"/api/v1/users/{uid}/tools/python", json={"depth": 5}).status_code == 422
    assert c.put(f"/api/v1/users/{uid}/tools/cobol", json={"depth": 1}).status_code == 404
    c.put(f"/api/v1/users/{uid}/tools/pandas", json={"depth": 2})
    mine = c.get(f"/api/v1/users/{uid}/tools").json()
    assert [x["tool"]["id"] for x in mine] == ["pandas", "python"]  # most recent first
    # the map remembers the chosen depth
    assert c.get("/api/v1/tools/python/map", params={"user_id": uid}).json()["depth"] == 4
    assert c.delete(f"/api/v1/users/{uid}/tools/pandas").status_code == 204
    assert [x["tool"]["id"] for x in c.get(f"/api/v1/users/{uid}/tools").json()] == ["python"]


def test_blocked_by_another_tool_suggests_that_lesson(c):
    uid = new_user(c)
    c.put(f"/api/v1/users/{uid}/tools/pandas", json={"depth": 2})
    nxt = c.get(f"/api/v1/users/{uid}/tools/pandas/next").json()
    assert nxt[0]["id"] == "python.lists" and nxt[0]["reason"] == "Needed for Series"
    put(c, uid, "python.lists", understanding=100)
    assert c.get(f"/api/v1/users/{uid}/tools/pandas/next").json()[0]["id"] == "pandas.series"


def test_mark_a_whole_tool_level_as_known(c):
    uid = new_user(c)
    r = c.post(f"/api/v1/users/{uid}/tools/python/known", json={"level": 1})
    assert r.json() == {"updated": 3}
    m = c.get("/api/v1/tools/python/map", params={"user_id": uid, "depth": 2}).json()
    assert m["progress"]["learned"] == 3 and m["progress"]["next"][0]["id"] == "python.comprehensions"
    assert c.post(f"/api/v1/users/{uid}/tools/python/known", json={"level": 1}).json() == {"updated": 0}


def test_bulk_marking(c):
    uid = new_user(c)
    r = c.post(f"/api/v1/users/{uid}/progress/bulk",
               json={"lesson_ids": ["python.basics", "python.lists", "python.basics"], "understanding": 100})
    assert r.json() == {"updated": 2}
    assert c.post(f"/api/v1/users/{uid}/progress/bulk", json={"lesson_ids": ["python.nope"],
                                                              "understanding": 100}).status_code == 422
    assert c.post(f"/api/v1/users/{uid}/progress/bulk", json={"lesson_ids": ["python.basics"]}).status_code == 422
    assert c.post(f"/api/v1/users/{uid}/progress/bulk", json={"lesson_ids": []}).status_code == 422


def test_finishing_a_tool_is_announced(c):
    uid = new_user(c)
    c.put(f"/api/v1/users/{uid}/tools/python", json={"depth": 1})
    put(c, uid, "python.basics", understanding=100)
    assert put(c, uid, "python.lists", understanding=100)["finished_tool"] is None
    done = put(c, uid, "python.loops", understanding=100)["finished_tool"]
    assert done["id"] == "python" and done["depth_name"] == "Essentials"


def test_reviews_schedule_and_spacing(c):
    uid = new_user(c)
    put(c, uid, "python.basics", understanding=100)
    assert c.get(f"/api/v1/users/{uid}/reviews").json()["due"] == 0  # first review tomorrow
    # pretend it's due now
    from app.userdb import session

    with session() as conn:
        conn.execute("UPDATE reviews SET due_at = ? WHERE user_id = ?", (time.time() - 1, uid))
    due = c.get(f"/api/v1/users/{uid}/reviews").json()
    assert due["due"] == 1 and due["items"][0]["id"] == "python.basics" and due["items"][0]["quiz"]
    r1 = c.post(f"/api/v1/users/{uid}/reviews/python.basics", json={"correct": True}).json()
    r2 = c.post(f"/api/v1/users/{uid}/reviews/python.basics", json={"correct": True}).json()
    assert r1["interval_days"] == 3.0 and r2["interval_days"] == 6.9
    r3 = c.post(f"/api/v1/users/{uid}/reviews/python.basics", json={"correct": False}).json()
    assert r3["interval_days"] == 1.0
    assert c.get(f"/api/v1/users/{uid}/reviews").json()["due"] == 0
    # forgetting it (below 100%) removes it from reviews
    put(c, uid, "python.basics", understanding=50)
    with session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0


def test_feed_and_stats(c):
    uid = new_user(c)
    other = new_user(c, "Bob")
    c.put(f"/api/v1/users/{other}/tools/pandas", json={"depth": 1})
    c.put(f"/api/v1/users/{uid}/tools/python", json={"depth": 2})
    put(c, uid, "python.basics", understanding=100)
    c.put(f"/api/v1/users/{uid}/saved/python.gil")
    f = c.get(f"/api/v1/users/{uid}/feed").json()
    assert f["today"]["learned"] == 1 and f["today"]["goal"] == 2 and f["today"]["streak_days"] == 1
    assert f["learning"][0]["tool"]["id"] == "python" and f["learning"][0]["done"] == 1
    assert {n["id"] for n in f["up_next"]} == {"python.lists", "python.loops"}
    assert [s["id"] for s in f["saved"]] == ["python.gil"]
    assert f["trending"][0]["id"] == "pandas" and f["trending"][0]["learners"] == 1
    assert f["reviews_due"] == 0
    s = c.get(f"/api/v1/users/{uid}/stats").json()
    assert s["learned"] == 1 and s["minutes_learned"] == 10 and s["streak_days"] == 1
    assert s["tools"][0]["tool"]["id"] == "python" and s["tools"][0]["percent"] == 25
    assert len(s["weekly"]) == 8 and len(s["daily"]) == 7 and s["daily"][-1]["learned"] == 1


def test_new_programmers_default_to_essentials(c):
    uid = new_user(c)
    c.patch(f"/api/v1/users/{uid}", json={"experience": "new"})
    assert c.get("/api/v1/tools/python/map", params={"user_id": uid}).json()["depth"] == 1
    assert c.get("/api/v1/tools/python", params={"user_id": uid}).json()["progress"]["depth"] == 1


def test_tool_page_shows_progress_on_prerequisite_tools(c):
    uid = new_user(c)
    put(c, uid, "python.basics", understanding=100)
    t = c.get("/api/v1/tools/pandas", params={"user_id": uid}).json()
    assert t["enrolled"] is False and t["requires"][0]["progress"]["done"] == 1
    assert t["requires"][0]["progress"]["total"] == 3


def test_someone_elses_journey_is_private(c):
    uid, bob = new_user(c), new_user(c, "Bob")
    as_bob = bearer(TOKENS[bob])
    for method, url, kw in [
        ("get", f"/api/v1/users/{uid}/tools", {}),
        ("put", f"/api/v1/users/{uid}/tools/python", {"json": {"depth": 1}}),
        ("get", f"/api/v1/users/{uid}/feed", {}),
        ("get", f"/api/v1/users/{uid}/reviews", {}),
        ("post", f"/api/v1/users/{uid}/progress/bulk", {"json": {"lesson_ids": ["python.basics"], "understanding": 1}}),
        ("get", "/api/v1/tools/python/map?user_id=" + uid, {}),
    ]:
        assert getattr(c, method)(url, headers=as_bob, **kw).status_code == 403, url
