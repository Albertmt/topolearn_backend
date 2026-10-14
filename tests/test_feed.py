from conftest import concepts, edges, new_user

DATA = {
    "concepts": concepts("goal", "mid", "base", "side", "field_x", "fx_leaf"),
    "prerequisites": edges("goal>mid", "mid>base", "side>base", "field_x>fx_leaf"),
}


def test_empty_feed_for_new_user(make_client):
    c = make_client(DATA)
    uid = new_user(c)
    f = c.get(f"/api/v1/users/{uid}/feed").json()
    assert f == {"continue_learning": [], "ready_next": [], "recently_mastered": [], "popular": []}
    assert c.get("/api/v1/users/ghost/feed").status_code == 404


def test_feed_sections(make_client):
    c = make_client(DATA)
    me, other = new_user(c, "Me"), new_user(c, "Other")
    c.patch(f"/api/v1/users/{me}", json={"interests": ["field_x"]})
    c.put(f"/api/v1/users/{me}/goals/goal")
    c.put(f"/api/v1/users/{me}/progress/base", json={"understanding": 100})
    c.put(f"/api/v1/users/{me}/progress/side", json={"understanding": 40})
    c.put(f"/api/v1/users/{me}/progress/goal", json={"status": "need_to_learn"})
    c.put(f"/api/v1/users/{other}/progress/side", json={"understanding": 10})
    c.put(f"/api/v1/users/{other}/progress/base", json={"understanding": 50})

    f = c.get(f"/api/v1/users/{me}/feed").json()
    assert [x["id"] for x in f["continue_learning"]] == ["goal", "side"]  # newest first
    assert f["continue_learning"][1]["understanding"] == 40
    assert [x["id"] for x in f["recently_mastered"]] == ["base"]
    ready = {x["id"]: x["reason"] for x in f["ready_next"]}
    assert ready["mid"] == "For your goal: Goal"      # from the goal's path
    assert ready["fx_leaf"] == "In: Field X"          # then from interests
    assert "goal" not in ready                         # already in "continue"
    # popular: what others touched this week, minus what I already know
    assert [x["id"] for x in f["popular"]] == ["side"]
    assert f["popular"][0]["count"] == 1


def test_feed_skips_hidden_and_removed(make_client):
    c = make_client(DATA)
    uid = new_user(c)
    c.put(f"/api/v1/users/{uid}/goals/goal")
    c.put(f"/api/v1/users/{uid}/progress/base", json={"status": "hidden"})
    f = c.get(f"/api/v1/users/{uid}/feed").json()
    assert "base" not in [x["id"] for x in f["ready_next"]]
    assert f["continue_learning"] == []  # hidden isn't "in progress"
