import time

from conftest import concepts, edges, new_user

# Diamond-ish: target needs mid_easy (0.2) and mid_hard (0.6); both need base.
DATA = {
    "concepts": [
        {"id": "target", "name": "Target", "difficulty": 0.8},
        {"id": "mid_easy", "name": "Mid Easy", "difficulty": 0.2},
        {"id": "mid_hard", "name": "Mid Hard", "difficulty": 0.6},
        {"id": "base", "name": "Base", "difficulty": 0.1},
        {"id": "other", "name": "Other", "difficulty": 0.3},
    ],
    "prerequisites": [
        {"concept": "target", "requires": "mid_easy", "why": "Target builds on the easy middle."},
        {"concept": "target", "requires": "mid_hard"},
        {"concept": "mid_easy", "requires": "base"},
        {"concept": "mid_hard", "requires": "base"},
        {"concept": "other", "requires": "base"},
    ],
}


def _ids(path):
    return [s["id"] for s in path["steps"]]


# ------------------------------------------------------------------ path


def test_path_is_in_study_order(make_client):
    c = make_client(DATA)
    p = c.get("/api/v1/concepts/target/path").json()
    assert _ids(p) == ["base", "mid_easy", "mid_hard", "target"]  # easier first when both ready
    assert p["total"] == 4 and p["done"] == 0 and p["remaining"] == 4
    assert p["steps"][0]["ready"] is True  # a leaf needs nothing, so it is ready even anonymously
    assert all(s["minutes"] >= 20 for s in p["steps"])
    assert p["steps"][-1]["requires"] == ["mid_easy", "mid_hard"]
    assert "other" not in _ids(p)  # not a prerequisite


def test_path_marks_done_and_ready_and_estimates_time(make_client):
    c = make_client(DATA)
    uid = new_user(c)
    full = c.get("/api/v1/concepts/target/path", params={"user_id": uid}).json()
    assert [s["ready"] for s in full["steps"]] == [True, False, False, False]  # only the leaf is ready

    c.put(f"/api/v1/users/{uid}/progress/base", json={"understanding": 100})
    c.put(f"/api/v1/users/{uid}/progress/mid_hard", json={"understanding": 50})
    p = c.get("/api/v1/concepts/target/path", params={"user_id": uid}).json()
    by_id = {s["id"]: s for s in p["steps"]}
    assert by_id["base"]["done"] and not by_id["base"]["ready"]
    assert by_id["mid_easy"]["ready"] and by_id["mid_hard"]["ready"]
    assert not by_id["target"]["ready"]
    assert p["done"] == 1 and p["remaining"] == 3
    expected = by_id["mid_easy"]["minutes"] + round(by_id["mid_hard"]["minutes"] * 0.5) + by_id["target"]["minutes"]
    assert p["remaining_minutes"] == expected < full["remaining_minutes"]


def test_path_skips_hidden_concepts(make_client):
    c = make_client(DATA)
    uid = new_user(c)
    c.put(f"/api/v1/users/{uid}/progress/mid_hard", json={"status": "hidden"})
    p = c.get("/api/v1/concepts/target/path", params={"user_id": uid}).json()
    assert _ids(p) == ["base", "mid_easy", "target"]  # base stays: mid_easy still needs it


def test_path_unknown_concept(make_client):
    c = make_client(DATA)
    assert c.get("/api/v1/concepts/nope/path").status_code == 404


def test_graph_edges_carry_why_and_concepts_carry_example(make_client):
    data = {**DATA, "concepts": [dict(x, example="Like stacking blocks.") if x["id"] == "target" else x
                                 for x in DATA["concepts"]]}
    c = make_client(data)
    g = c.get("/api/v1/concepts/target/graph").json()
    why = {(e["concept"], e["requires"]): e["why"] for e in g["edges"]}
    assert why[("target", "mid_easy")] == "Target builds on the easy middle."
    assert why[("target", "mid_hard")] is None  # missing reason is fine
    assert c.get("/api/v1/concepts/target").json()["example"] == "Like stacking blocks."
    assert c.get("/api/v1/concepts/base").json()["example"] is None


# ------------------------------------------------------------- unlocking


def test_mastering_a_concept_reports_what_became_ready(make_client):
    c = make_client(DATA)
    uid = new_user(c)
    r = c.put(f"/api/v1/users/{uid}/progress/base", json={"understanding": 100}).json()
    assert {x["id"] for x in r["newly_ready"]} == {"mid_easy", "mid_hard", "other"}

    # target needs both middles: only the second one unlocks it
    assert c.put(f"/api/v1/users/{uid}/progress/mid_easy", json={"understanding": 100}).json()["newly_ready"] == []
    r = c.put(f"/api/v1/users/{uid}/progress/mid_hard", json={"understanding": 100}).json()
    assert [x["id"] for x in r["newly_ready"]] == ["target"]

    # re-saving an already-mastered concept doesn't celebrate again
    assert c.put(f"/api/v1/users/{uid}/progress/mid_hard", json={"understanding": 100}).json()["newly_ready"] == []
    # partial progress never unlocks anything
    assert c.put(f"/api/v1/users/{uid}/progress/other", json={"understanding": 90}).json()["newly_ready"] == []


def test_hidden_or_known_dependents_are_not_announced(make_client):
    c = make_client(DATA)
    uid = new_user(c)
    c.put(f"/api/v1/users/{uid}/progress/mid_easy", json={"status": "hidden"})
    c.put(f"/api/v1/users/{uid}/progress/other", json={"understanding": 100})
    r = c.put(f"/api/v1/users/{uid}/progress/base", json={"understanding": 100}).json()
    assert [x["id"] for x in r["newly_ready"]] == ["mid_hard"]


# ------------------------------------------------------------------ goals


def test_goals_lifecycle_and_summary(make_client):
    c = make_client(DATA)
    uid = new_user(c)
    assert c.get(f"/api/v1/users/{uid}/goals").json() == []
    g = c.put(f"/api/v1/users/{uid}/goals/target").json()
    assert g["concept"] == {"id": "target", "name": "Target"}
    assert g["total"] == 4 and g["done"] == 0
    assert [n["id"] for n in g["next"]] == ["base"]

    c.put(f"/api/v1/users/{uid}/progress/base", json={"understanding": 100})
    g = c.get(f"/api/v1/users/{uid}/goals").json()[0]
    assert g["done"] == 1 and [n["id"] for n in g["next"]] == ["mid_easy", "mid_hard"]

    # idempotent add, then remove
    assert c.put(f"/api/v1/users/{uid}/goals/target").status_code == 200
    assert len(c.get(f"/api/v1/users/{uid}/goals").json()) == 1
    assert c.delete(f"/api/v1/users/{uid}/goals/target").status_code == 204
    assert c.get(f"/api/v1/users/{uid}/goals").json() == []


def test_goal_limit_and_validation(make_client):
    ids = [f"g{i}" for i in range(7)]
    c = make_client({"concepts": concepts(*ids), "prerequisites": []})
    uid = new_user(c)
    for i in ids[:5]:
        assert c.put(f"/api/v1/users/{uid}/goals/{i}").status_code == 200
    r = c.put(f"/api/v1/users/{uid}/goals/g5")
    assert r.status_code == 409 and "5 goals" in r.json()["detail"]
    assert c.put(f"/api/v1/users/{uid}/goals/nope").status_code == 404
    assert c.put("/api/v1/users/nobody/goals/g0").status_code == 404
    assert c.get("/api/v1/users/nobody/goals").status_code == 404


# ------------------------------------------------------------------ stats


def _backdate_events(uid, days_ago):
    """Rewrite the user's progress events to happen N days ago (one per entry)."""
    from app.userdb import session

    with session() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM progress_events WHERE user_id = ? ORDER BY id", (uid,))]
        for eid, d in zip(ids, days_ago):
            conn.execute("UPDATE progress_events SET at = ? WHERE id = ?", (time.time() - d * 86400, eid))


def test_stats_counts_streaks_and_weeks(make_client):
    c = make_client({"concepts": concepts("a", "b", "c", "d", "e"), "prerequisites": []})
    uid = new_user(c)
    empty = c.get(f"/api/v1/users/{uid}/stats").json()
    assert empty["understood"] == 0 and empty["streak_days"] == 0 and empty["best_streak"] == 0
    assert len(empty["weekly"]) == 8 and all(w["mastered"] == 0 for w in empty["weekly"])

    c.put(f"/api/v1/users/{uid}/progress/a", json={"understanding": 100})  # 5 days ago
    c.put(f"/api/v1/users/{uid}/progress/b", json={"understanding": 100})  # 1 day ago
    c.put(f"/api/v1/users/{uid}/progress/c", json={"understanding": 40})   # today
    c.put(f"/api/v1/users/{uid}/progress/d", json={"status": "need_to_learn"})  # today
    c.put(f"/api/v1/users/{uid}/progress/e", json={"status": "hidden"})  # today
    _backdate_events(uid, [5, 1, 0, 0, 0])

    s = c.get(f"/api/v1/users/{uid}/stats").json()
    assert (s["understood"], s["partly"], s["need_to_learn"], s["hidden"]) == (2, 1, 1, 1)
    assert s["streak_days"] == 2  # yesterday + today
    assert s["best_streak"] == 2
    assert sum(w["mastered"] for w in s["weekly"]) == 2


def test_streak_broken_when_inactive(make_client):
    c = make_client({"concepts": concepts("a", "b"), "prerequisites": []})
    uid = new_user(c)
    c.put(f"/api/v1/users/{uid}/progress/a", json={"understanding": 100})
    c.put(f"/api/v1/users/{uid}/progress/b", json={"understanding": 100})
    _backdate_events(uid, [4, 3])
    s = c.get(f"/api/v1/users/{uid}/stats").json()
    assert s["streak_days"] == 0 and s["best_streak"] == 2


def test_stats_validation(make_client):
    c = make_client({"concepts": concepts("a"), "prerequisites": []})
    uid = new_user(c)
    assert c.get(f"/api/v1/users/{uid}/stats", params={"tz_offset_minutes": -420}).status_code == 200
    assert c.get(f"/api/v1/users/{uid}/stats", params={"tz_offset_minutes": 99999}).status_code == 422
    assert c.get("/api/v1/users/nobody/stats").status_code == 404
