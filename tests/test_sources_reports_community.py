from conftest import concepts, edges, new_user

DATA = {
    "concepts": concepts("calc", "limits", "derivative", "integral"),
    "prerequisites": edges("calc>derivative", "calc>integral", "derivative>limits", "integral>limits"),
}


def _add(c, uid, cid="derivative", url="https://ex.org/a", **kw):
    r = c.post(f"/api/v1/concepts/{cid}/sources", json={"user_id": uid, "url": url, **kw})
    assert r.status_code == 201, r.text
    return r.json()


# --------------------------------------------------------------- sources


def test_source_also_covers_other_concepts(make_client):
    c = make_client(DATA)
    uid = new_user(c)
    s = _add(c, uid, title="Calculus course", kind="course", also_covers=["integral", "limits", "derivative"])
    assert [x["id"] for x in s["covers"]] == ["derivative", "integral", "limits"]  # own concept first
    # listed on every concept it covers
    for cid in ("derivative", "integral", "limits"):
        assert [x["id"] for x in c.get(f"/api/v1/concepts/{cid}/sources").json()] == [s["id"]]
    assert c.get("/api/v1/concepts/calc/sources").json() == []

    bad = c.post("/api/v1/concepts/derivative/sources",
                 json={"user_id": uid, "url": "https://ex.org/b", "also_covers": ["nope"]})
    assert bad.status_code == 422


def test_kind_filter(make_client):
    c = make_client(DATA)
    uid = new_user(c)
    _add(c, uid, url="https://v.org", kind="video")
    _add(c, uid, url="https://b.org", kind="book")
    assert [s["kind"] for s in c.get("/api/v1/concepts/derivative/sources", params={"kind": "video"}).json()] == ["video"]
    assert c.get("/api/v1/concepts/derivative/sources", params={"kind": "paper"}).json() == []


def test_helped_ranks_sources_higher(make_client):
    c = make_client(DATA)
    ada, bob, cy = new_user(c, "Ada"), new_user(c, "Bob"), new_user(c, "Cy")
    first = _add(c, ada, url="https://first.org")
    second = _add(c, bob, url="https://second.org")
    c.put(f"/api/v1/sources/{first['id']}/vote", json={"user_id": cy, "value": 1})  # first: score 2
    r = c.put(f"/api/v1/sources/{second['id']}/helped", json={"user_id": cy, "helped": True}).json()
    assert r["helped_count"] == 1 and r["helped_by_me"] is True
    # second: score 1 + 2*1 helped = 3 > 2
    assert [s["id"] for s in c.get("/api/v1/concepts/derivative/sources").json()] == [second["id"], first["id"]]
    # idempotent, and can be taken back
    c.put(f"/api/v1/sources/{second['id']}/helped", json={"user_id": cy, "helped": True})
    r = c.put(f"/api/v1/sources/{second['id']}/helped", json={"user_id": cy, "helped": False}).json()
    assert r["helped_count"] == 0 and r["helped_by_me"] is False
    assert c.put("/api/v1/sources/nope/helped", json={"user_id": cy, "helped": True}).status_code == 404


def test_path_suggests_sources_covering_several_steps(make_client):
    c = make_client(DATA)
    uid = new_user(c)
    big = _add(c, uid, url="https://big.org", also_covers=["integral", "limits"])
    _add(c, uid, url="https://small.org")  # covers only 1 step: not suggested
    p = c.get("/api/v1/concepts/calc/path", params={"user_id": uid}).json()
    assert [s["id"] for s in p["covering_sources"]] == [big["id"]]
    assert p["covering_sources"][0]["covers"] == 3
    # once those are understood, the suggestion goes away
    for cid in ("limits", "derivative", "integral"):
        c.put(f"/api/v1/users/{uid}/progress/{cid}", json={"understanding": 100})
    assert c.get("/api/v1/concepts/calc/path", params={"user_id": uid}).json()["covering_sources"] == []


def test_url_normalisation_and_rejections(make_client):
    c = make_client(DATA)
    uid = new_user(c)
    assert _add(c, uid, url="  example.com/x  ")["url"] == "https://example.com/x"
    assert _add(c, uid, url="http://plain.org")["url"] == "http://plain.org"
    for bad in ["", "not a link", "ftp://files.org/x", "https://nodot", "javascript:alert(1)", "https://a b.com"]:
        r = c.post("/api/v1/concepts/derivative/sources", json={"user_id": uid, "url": bad})
        assert r.status_code == 422, bad
    long_title = "x" * 201
    assert c.post("/api/v1/concepts/derivative/sources",
                  json={"user_id": uid, "url": "https://t.org", "title": long_title}).status_code == 422
    assert c.post("/api/v1/concepts/derivative/sources",
                  json={"user_id": "ghost", "url": "https://g.org"}).status_code == 404
    assert c.post("/api/v1/concepts/nope/sources", json={"user_id": uid, "url": "https://g.org"}).status_code == 404


# ------------------------------------------------------------- community


def test_community_counts(make_client):
    c = make_client(DATA)
    a, b, d, e = (new_user(c, n) for n in "ABDE")
    c.put(f"/api/v1/users/{a}/progress/derivative", json={"understanding": 100})
    c.put(f"/api/v1/users/{b}/progress/derivative", json={"understanding": 30})
    c.put(f"/api/v1/users/{d}/progress/derivative", json={"status": "need_to_learn"})
    c.put(f"/api/v1/users/{e}/progress/derivative", json={"understanding": 10, "status": "hidden"})  # not counted
    c.put(f"/api/v1/users/{a}/goals/derivative")
    m = c.get("/api/v1/concepts/derivative/community").json()
    assert (m["learning"], m["understood"], m["goal_of"]) == (2, 1, 1)
    assert m["top_source"] is None
    s = _add(c, a, url="https://best.org")
    assert c.get("/api/v1/concepts/derivative/community").json()["top_source"]["id"] == s["id"]
    zero = c.get("/api/v1/concepts/limits/community").json()
    assert (zero["learning"], zero["understood"], zero["goal_of"]) == (0, 0, 0)
    assert c.get("/api/v1/concepts/nope/community").status_code == 404


# --------------------------------------------------------------- reports


def test_reports(make_client):
    c = make_client(DATA)
    uid = new_user(c)
    r = c.post("/api/v1/reports", json={
        "user_id": uid, "kind": "wrong_prerequisite", "concept_id": "calc", "related_concept_id": "integral",
        "context_concept_id": "calc", "message": "Calculus overview doesn't need integrals first",
        "suggestion": "Remove this link",
    })
    assert r.status_code == 201, r.text
    rep = r.json()
    assert rep["concept"]["name"] == "Calc" and rep["related_concept"]["id"] == "integral"
    assert rep["status"] == "open"

    # anonymous / unknown users are fine (report still useful)
    assert c.post("/api/v1/reports", json={"user_id": "ghost", "kind": "other", "concept_id": "calc",
                                            "message": "typo"}).status_code == 201
    listed = c.get("/api/v1/reports").json()
    assert len(listed) == 2 and listed[0]["kind"] == "other"  # newest first


def test_report_validation(make_client):
    c = make_client(DATA)
    base = {"concept_id": "calc"}
    assert c.post("/api/v1/reports", json={**base, "kind": "bogus", "message": "x"}).status_code == 422
    assert c.post("/api/v1/reports", json={**base, "kind": "other"}).status_code == 422  # nothing said
    assert c.post("/api/v1/reports", json={**base, "kind": "other", "message": "   "}).status_code == 422
    assert c.post("/api/v1/reports", json={**base, "kind": "missing_prerequisite"}).status_code == 422
    assert c.post("/api/v1/reports", json={**base, "kind": "duplicate", "related_concept_id": "nope"}).status_code == 404
    assert c.post("/api/v1/reports", json={"concept_id": "nope", "kind": "other", "message": "x"}).status_code == 404
    assert c.post("/api/v1/reports", json={**base, "kind": "other", "message": "x" * 2001}).status_code == 422
    assert c.post("/api/v1/reports", json={**base, "kind": "wrong_description",
                                            "suggestion": "Better text"}).status_code == 201


def test_deleting_user_keeps_reports_and_shared_sources(make_client):
    c = make_client(DATA)
    uid = new_user(c)
    _add(c, uid)
    c.post("/api/v1/reports", json={"user_id": uid, "kind": "other", "concept_id": "calc", "message": "x"})
    c.put(f"/api/v1/users/{uid}/goals/calc")
    assert c.delete(f"/api/v1/users/{uid}").status_code == 204
    assert len(c.get("/api/v1/reports").json()) == 1
    assert len(c.get("/api/v1/concepts/derivative/sources").json()) == 1
    assert c.get("/api/v1/concepts/calc/community").json()["goal_of"] == 0


# ------------------------------------------------------------ share page


def test_share_page_renders_path_and_escapes_html(make_client):
    data = {
        "concepts": [
            {"id": "x", "name": "<script>alert(1)</script>", "description": "a & b"},
            {"id": "y", "name": "Basics", "description": "<b>bold</b>"},
        ],
        "prerequisites": edges("x>y"),
    }
    c = make_client(data)
    r = c.get("/p/x")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    body = r.text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body and "a &amp; b" in body and "&lt;b&gt;bold" in body
    assert body.index("Basics") < body.rindex("&lt;script&gt;")  # foundations first
    assert c.get("/p/nope").status_code == 404
