"""The catalog: tools, maps by depth, lessons, search, share page, loading."""
import pytest

from conftest import load_graph, run_loader, tool_doc

PY = tool_doc(
    "python", ["basics", "lists", "loops", "comprehensions", "generators", "gil"],
    edges=["lists>basics", "loops>basics", "comprehensions>lists", "comprehensions>loops",
           "generators>comprehensions", "gil>generators"],
    levels={"comprehensions": 2, "generators": 3, "gil": 4},
    name="Python", aliases=["py", "python3"], icon="Py",
)
PANDAS = tool_doc(
    "pandas", ["series", "dataframes", "groupby"],
    edges=["series>python.lists", "dataframes>series", "dataframes>python.comprehensions", "groupby>dataframes"],
    levels={"groupby": 2}, requires=["python:1"], name="pandas", category="library", icon="pd",
)
GIT = tool_doc("git", ["commits", "branches"], edges=["branches>commits"], name="Git", category="tool")


@pytest.fixture()
def client(make_client):
    return make_client([PY, PANDAS, GIT])


def test_health(client):
    assert client.get("/health").json() == {"status": "ok", "tools": 3, "lessons": 11}


def test_tools_in_catalog_order_with_level_stats(client):
    tools = client.get("/api/v1/tools").json()
    assert [t["id"] for t in tools] == ["python", "pandas", "git"]
    py = tools[0]
    assert py["lessons"] == 6 and py["minutes"] == 60 and py["learners"] == 0
    assert [lv["lessons"] for lv in py["levels"]] == [3, 1, 1, 1]
    assert [lv["total_lessons"] for lv in py["levels"]] == [3, 4, 5, 6]
    assert py["levels"][1]["name"] == "Practical"
    assert [t["id"] for t in client.get("/api/v1/tools", params={"category": "tool"}).json()] == ["git"]


def test_tool_detail_with_tool_prerequisites(client):
    pd = client.get("/api/v1/tools/pandas").json()
    assert pd["requires"][0]["id"] == "python" and pd["requires"][0]["level_name"] == "Essentials"
    assert pd["modules"][0] == {"id": "basics", "name": "Basics"}
    py = client.get("/api/v1/tools/python").json()
    assert [t["id"] for t in py["required_by"]] == ["pandas"]
    assert client.get("/api/v1/tools/cobol").status_code == 404


def test_map_respects_depth(client):
    m = client.get("/api/v1/tools/python/map", params={"depth": 1}).json()
    assert {n["id"] for n in m["nodes"]} == {"python.basics", "python.lists", "python.loops"}
    assert m["depth"] == 1
    m4 = client.get("/api/v1/tools/python/map", params={"depth": 4}).json()
    assert len(m4["nodes"]) == 6
    assert {"concept": "python.gil", "requires": "python.generators"} in m4["edges"]
    # foundations first in the study order
    assert m4["order"][0] == "python.basics" and m4["order"][-1] == "python.gil"
    # default depth when signed out is Practical
    assert client.get("/api/v1/tools/python/map").json()["depth"] == 2


def test_map_shows_other_tools_lessons_it_builds_on(client):
    m = client.get("/api/v1/tools/pandas/map", params={"depth": 2}).json()
    ext = [n for n in m["nodes"] if n["external"]]
    assert {n["id"] for n in ext} == {"python.lists", "python.comprehensions"}
    assert ext[0]["tool"]["id"] == "python"
    assert {"concept": "pandas.series", "requires": "python.lists"} in m["edges"]
    assert "python.lists" not in m["order"]  # the order only covers the tool's own lessons


def test_lesson_detail(client):
    les = client.get("/api/v1/lessons/python.comprehensions").json()
    assert les["level"] == 2 and les["level_name"] == "Practical"
    assert les["module_name"] == "Basics" and les["tool"]["id"] == "python"
    assert {r["id"] for r in les["requires"]} == {"python.lists", "python.loops"}
    assert {u["id"] for u in les["unlocks"]} == {"python.generators", "pandas.dataframes"}
    assert les["unlocks"][0]["tool"]["id"] == "python"  # own tool first
    assert les["quiz"][0]["options"] == ["it", "not it"] and les["takeaways"] == ["one", "two"]
    assert les["next_in_tool"]["id"] == "python.generators"
    assert les["social"] == {"up": 0, "down": 0, "mine": 0, "saved": False, "comments": 0, "learned_by": 0}
    assert client.get("/api/v1/lessons/python.nope").status_code == 404


def test_search_tools_then_lessons(client):
    r = client.get("/api/v1/search", params={"q": "py"}).json()
    assert r["tools"][0]["id"] == "python"  # alias
    r = client.get("/api/v1/search", params={"q": "pan"}).json()
    assert [t["id"] for t in r["tools"]] == ["pandas"]
    r = client.get("/api/v1/search", params={"q": "lists"}).json()
    assert r["lessons"][0]["id"] == "python.lists"
    r = client.get("/api/v1/search", params={"q": "python gen"}).json()  # tool name + lesson prefix
    assert [x["id"] for x in r["lessons"]] == ["python.generators"]
    r = client.get("/api/v1/search", params={"q": "branches", "tool": "python"}).json()
    assert r["tools"] == [] and r["lessons"] == []


@pytest.mark.parametrize("q", ["%", "_", "'", "\\", "a & b", ":*", "!!", "   x  ", "日本"])
def test_search_survives_odd_queries(client, q):
    assert client.get("/api/v1/search", params={"q": q}).status_code == 200


@pytest.mark.parametrize("lid", ["nope", "python.a'; DROP TABLE x;--", "..", "ünï"])
def test_unknown_or_hostile_ids_are_404(client, lid):
    assert client.get(f"/api/v1/lessons/{lid}").status_code == 404
    assert client.get(f"/api/v1/tools/{lid}/map").status_code == 404


def test_share_page_lists_the_curriculum_and_escapes(make_client):
    doc = tool_doc("x", ["a", "b"], edges=["b>a"])
    doc["concepts"][1]["name"] = "<script>alert(1)</script>"
    c = make_client(doc)
    r = c.get("/t/x", params={"depth": 1})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "<script>alert(1)</script>" not in r.text and "&lt;script&gt;" in r.text
    assert r.text.index("About a.") < r.text.index("About b.")
    assert c.get("/t/nope").status_code == 404


# ---------------------------------------------------------------- loading


def test_loader_rejects_a_lesson_needing_a_deeper_one(tmp_path, make_client):
    make_client(GIT)
    bad = tool_doc("x", ["a", "b"], edges=["a>b"], levels={"b": 3})
    res = run_loader(bad, tmp_path)
    assert res.returncode != 0 and "deeper lesson" in (res.stderr + res.stdout)


def test_loader_rejects_loops_and_keeps_the_live_catalog(tmp_path, make_client):
    c = make_client(GIT)
    res = run_loader(tool_doc("x", ["a", "b"], edges=["a>b", "b>a"]), tmp_path)
    assert res.returncode != 0 and "loop" in (res.stderr + res.stdout)
    assert c.get("/api/v1/tools/git").status_code == 200
    assert c.get("/api/v1/tools/x").status_code == 404


def test_loader_drops_unknown_prerequisites(make_client):
    c = make_client(tool_doc("x", ["a"], edges=["a>python.missing"]))
    assert c.get("/api/v1/lessons/x.a").json()["requires"] == []


def test_reload_swaps_the_catalog_and_keeps_user_data(make_client):
    from conftest import new_user

    c = make_client([PY, GIT])
    uid = new_user(c)
    c.put(f"/api/v1/users/{uid}/progress/git.commits", json={"understanding": 100})
    load_graph([GIT, tool_doc("rust", ["ownership"], name="Rust")])
    assert c.get("/api/v1/tools/python").status_code == 404
    assert c.get("/api/v1/tools/rust").status_code == 200
    m = c.get("/api/v1/tools/git/map", params={"user_id": uid, "depth": 1}).json()
    assert {n["id"]: n["understanding"] for n in m["nodes"]}["git.commits"] == 100


def test_the_real_content_loads(make_client):
    """Every curriculum in data/tools validates and loads together."""
    import json
    from pathlib import Path

    files = sorted((Path(__file__).resolve().parent.parent / "data" / "tools").glob("*.json"))
    if not files:
        pytest.skip("no content files yet")
    docs = [json.loads(f.read_text()) for f in files]
    c = make_client(docs)
    tools = c.get("/api/v1/tools").json()
    assert len(tools) == len(files)
    for t in tools:
        m = c.get(f"/api/v1/tools/{t['id']}/map", params={"depth": 4}).json()
        assert len(m["order"]) == t["lessons"], t["id"]


def test_collections_group_tools_in_curated_order(make_client):
    from conftest import TEST_URL
    from scripts.build_db import load_into

    c = make_client([tool_doc("python", ["Basics"]), tool_doc("flask", ["Routes"]), tool_doc("zig", ["Hello"])])
    load_into(TEST_URL, [tool_doc("python", ["Basics"]), tool_doc("flask", ["Routes"]), tool_doc("zig", ["Hello"])],
              {"collections": [
                  {"id": "py", "name": "Python world", "tools": ["flask", "python", "nope"]},
                  {"id": "empty", "name": "Nothing yet", "tools": ["nope"]},
              ]})
    cols = c.get("/api/v1/collections").json()
    assert [x["id"] for x in cols] == ["py", "more"]  # empty ones are hidden
    assert [t["id"] for t in cols[0]["tools"]] == ["flask", "python"]  # curated order, unknown ids dropped
    assert cols[0]["tools"][0]["lessons"] == 1
    assert [t["id"] for t in cols[1]["tools"]] == ["zig"]  # not in any collection
    # The catalog follows the collections' order.
    assert [t["id"] for t in c.get("/api/v1/tools").json()] == ["flask", "python", "zig"]


def test_collections_without_any_defined(make_client):
    c = make_client([tool_doc("python", ["Basics"])])
    cols = c.get("/api/v1/collections").json()
    assert [x["id"] for x in cols] == ["more"] and cols[0]["name"] == "All tools"


def test_collections_file_is_valid():
    import json
    from pathlib import Path

    data = json.loads((Path(__file__).resolve().parent.parent / "data" / "collections.json").read_text())
    ids = [c["id"] for c in data["collections"]]
    assert len(ids) == len(set(ids))
    assert all(c["name"] and c["tools"] for c in data["collections"])


def test_tool_logo_is_served_when_present(make_client):
    doc = tool_doc("python", ["Basics"])
    doc["tool"]["logo_svg"] = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M1 1h2"/></svg>'
    c = make_client([doc, tool_doc("zig", ["Hello"])])
    tools = {t["id"]: t for t in c.get("/api/v1/tools").json()}
    assert tools["python"]["logo_svg"].startswith("<svg")
    assert tools["zig"]["logo_svg"] is None
    assert c.get("/api/v1/tools/python").json()["logo_svg"].startswith("<svg")
    # Lesson refs stay small: no logo on the tool badge.
    m = c.get("/api/v1/tools/python/map", params={"depth": 1}).json()
    assert "logo_svg" not in m["nodes"][0]["tool"] and m["tool"]["logo_svg"].startswith("<svg")


def test_logo_entries_match_tools():
    import json
    from pathlib import Path

    from scripts.build_db import read_logos

    root = Path(__file__).resolve().parent.parent / "data"
    tools = {p.stem for p in (root / "tools").glob("*.json")}
    raw = json.loads((root / "logos.json").read_text())["logos"]
    for tid in raw:
        assert tid in tools, f"logo without a tool: {tid}"
    svgs = read_logos()
    assert set(svgs) == set(raw)
    assert all(v.startswith("<svg") and len(v) < 20_000 for v in svgs.values())
