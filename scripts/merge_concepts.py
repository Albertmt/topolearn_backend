#!/usr/bin/env python3
"""Fold duplicate concepts together and re-tidy the prerequisite graph.

    python scripts/merge_concepts.py                       # data/concepts.json + data/merges.txt, in place
    python scripts/merge_concepts.py --check               # only report, write nothing

For each "<keep> <drop>" line in the merges file:
  * every edge to/from <drop> is re-pointed to <keep>
  * <drop>'s name and aliases become aliases of <keep>
Then the whole graph is tidied:
  * self-loops and duplicate edges removed
  * cycles broken (drops the edge whose prerequisite is "hardest" relative to its concept)
  * transitively redundant edges removed (A→C is dropped when A→B→…→C exists)
  * difficulty smoothed so no concept is easier than its own prerequisites
Already-merged lines are skipped, so the script is safe to re-run.
"""
import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
Edge = Tuple[str, str]  # (concept, requires)


def read_merges(path: Path) -> List[Tuple[str, str]]:
    out = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            keep, drop = line.split()
            out.append((keep, drop))
    return out


def merge(concepts: Dict[str, dict], edges: Set[Edge], merges, whys: Dict[Edge, str] = None) -> int:
    target: Dict[str, str] = {}
    done = 0
    for keep, drop in merges:
        if drop not in concepts:
            continue  # already merged
        if keep not in concepts:
            sys.exit(f"merge target '{keep}' does not exist")
        k, d = concepts[keep], concepts.pop(drop)
        aliases = list(k.get("aliases") or [])
        seen = {a.lower() for a in aliases} | {k["name"].lower()}
        for a in [d["name"]] + list(d.get("aliases") or []):
            if a.lower() not in seen:
                seen.add(a.lower())
                aliases.append(a)
        k["aliases"] = aliases[:8]
        if not k.get("wiki_url") and d.get("wiki_url"):
            k["wiki_url"] = d["wiki_url"]
        target[drop] = keep
        done += 1

    def resolve(i: str) -> str:
        while i in target:
            i = target[i]
        return i

    if whys is not None:
        moved: Dict[Edge, str] = {}
        for (a, b), why in whys.items():
            key = (resolve(a), resolve(b))
            moved.setdefault(key, why)
        whys.clear()
        whys.update(moved)
    new = {(resolve(a), resolve(b)) for a, b in edges}
    edges.clear()
    edges.update((a, b) for a, b in new if a != b)
    return done


def _cycles(nodes, edges: Set[Edge]) -> List[List[str]]:
    """Tarjan SCC, iterative (graph can be deep)."""
    adj = defaultdict(list)
    for a, b in edges:
        adj[a].append(b)
    index, low, on, stack, out, counter = {}, {}, set(), [], [], 0
    for root in nodes:
        if root in index:
            continue
        work = [(root, 0)]
        while work:
            v, i = work.pop()
            if i == 0:
                index[v] = low[v] = counter
                counter += 1
                stack.append(v)
                on.add(v)
            recurse = False
            for j in range(i, len(adj[v])):
                w = adj[v][j]
                if w not in index:
                    work.append((v, j + 1))
                    work.append((w, 0))
                    recurse = True
                    break
                if w in on:
                    low[v] = min(low[v], index[w])
            if recurse:
                continue
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                if len(comp) > 1:
                    out.append(comp)
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
    return out


def break_cycles(concepts, edges: Set[Edge]) -> List[Edge]:
    diff = {i: float(c.get("difficulty") or 0) for i, c in concepts.items()}
    dependents = Counter(b for _, b in edges)
    cut = []
    while True:
        comps = _cycles(list(concepts), edges)
        if not comps:
            return cut
        for comp in comps:
            s = set(comp)
            inner = [(a, b) for a, b in edges if a in s and b in s]
            worst = max(inner, key=lambda e: (diff[e[1]] - diff[e[0]], dependents[e[0]] - dependents[e[1]], e))
            edges.discard(worst)
            cut.append(worst)


def topo_order(concepts, edges: Set[Edge]) -> List[str]:
    """Prerequisites first."""
    users, indeg = defaultdict(list), {i: 0 for i in concepts}
    for a, b in edges:
        users[b].append(a)
        indeg[a] += 1
    queue = sorted(i for i, d in indeg.items() if d == 0)
    order = []
    while queue:
        n = queue.pop()
        order.append(n)
        for u in users[n]:
            indeg[u] -= 1
            if indeg[u] == 0:
                queue.append(u)
    return order


def transitive_reduction(concepts, edges: Set[Edge]) -> int:
    adj = defaultdict(set)
    for a, b in edges:
        adj[a].add(b)
    reach: Dict[str, Set[str]] = {}
    for n in topo_order(concepts, edges):  # prerequisites are processed before their users
        r = set()
        for b in adj[n]:
            r.add(b)
            r |= reach[b]
        reach[n] = r
    redundant = {
        (a, b)
        for a in adj
        for b in adj[a]
        if any(c != b and b in reach[c] for c in adj[a])
    }
    edges -= redundant
    return len(redundant)


def smooth_difficulty(concepts, edges: Set[Edge]) -> int:
    req = defaultdict(list)
    for a, b in edges:
        req[a].append(b)
    changed = 0
    for n in topo_order(concepts, edges):
        if req[n]:
            m = max(float(concepts[p].get("difficulty") or 0) for p in req[n])
            if m > float(concepts[n].get("difficulty") or 0):
                concepts[n]["difficulty"] = round(min(1.0, m + 0.01), 2)
                changed += 1
    return changed


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", type=Path, default=ROOT / "data" / "concepts.json")
    p.add_argument("--merges", type=Path, default=ROOT / "data" / "merges.txt")
    p.add_argument("--check", action="store_true", help="report only, don't write")
    args = p.parse_args()

    data = json.loads(args.data.read_text(encoding="utf-8"))
    concepts = {c["id"]: c for c in data["concepts"]}
    edges = {(e["concept"], e["requires"]) for e in data["prerequisites"]}
    whys = {(e["concept"], e["requires"]): e["why"] for e in data["prerequisites"] if e.get("why")}
    before = (len(concepts), len(edges))

    merged = merge(concepts, edges, read_merges(args.merges), whys) if args.merges.exists() else 0
    dangling = {(a, b) for a, b in edges if a not in concepts or b not in concepts}
    edges -= dangling
    cut = break_cycles(concepts, edges)
    redundant = transitive_reduction(concepts, edges)
    smoothed = smooth_difficulty(concepts, edges)

    # popularity = how many concepts directly build on this one
    dependents = Counter(b for _, b in edges)
    for i, c in concepts.items():
        c["popularity"] = dependents[i]

    print(f"concepts {before[0]} → {len(concepts)} ({merged} merged)")
    print(f"edges    {before[1]} → {len(edges)} "
          f"({len(dangling)} dangling, {len(cut)} cycle cuts, {redundant} redundant removed)")
    for a, b in cut:
        print(f"  cycle cut: {a} -/-> {b}")
    print(f"difficulty raised on {smoothed} concepts")

    if args.check:
        return
    data["concepts"] = [concepts[i] for i in sorted(concepts, key=lambda i: (concepts[i].get("domain") or "", i))]
    data["prerequisites"] = [
        {"concept": a, "requires": b, **({"why": whys[(a, b)]} if (a, b) in whys else {})}
        for a, b in sorted(edges)
    ]
    args.data.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {args.data}")


if __name__ == "__main__":
    main()
