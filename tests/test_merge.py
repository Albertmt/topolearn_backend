import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "merge_concepts", Path(__file__).resolve().parent.parent / "scripts" / "merge_concepts.py")
mc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mc)


def _c(i, d=0.5):
    return {"id": i, "name": i.title(), "difficulty": d}


def test_merge_redirects_edges_and_aliases():
    concepts = {i: _c(i) for i in ["a", "b", "b2", "c"]}
    edges = {("a", "b2"), ("b2", "c"), ("b", "c")}
    assert mc.merge(concepts, edges, [("b", "b2")]) == 1
    assert "b2" not in concepts
    assert edges == {("a", "b"), ("b", "c")}
    assert "B2" in concepts["b"]["aliases"]
    assert mc.merge(concepts, edges, [("b", "b2")]) == 0  # idempotent


def test_cycles_are_broken_at_the_hardest_prerequisite():
    concepts = {"a": _c("a", 0.2), "b": _c("b", 0.5), "c": _c("c", 0.6)}
    edges = {("c", "b"), ("b", "a"), ("a", "c")}  # a "requires" the harder c: that's the bad one
    cut = mc.break_cycles(concepts, edges)
    assert cut == [("a", "c")]
    assert mc._cycles(list(concepts), edges) == []


def test_transitive_reduction_and_smoothing():
    concepts = {"a": _c("a", 0.1), "b": _c("b", 0.5), "c": _c("c", 0.3)}
    edges = {("c", "b"), ("b", "a"), ("c", "a")}
    assert mc.transitive_reduction(concepts, edges) == 1
    assert edges == {("c", "b"), ("b", "a")}
    assert mc.smooth_difficulty(concepts, edges) == 1
    assert concepts["c"]["difficulty"] == 0.51
