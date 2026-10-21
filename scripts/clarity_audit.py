#!/usr/bin/env python3
"""Clarity audit: what each lesson may assume, and which terms it uses that the
learner hasn't been taught yet.

    python3 scripts/clarity_audit.py docker            # one tool, every lesson
    python3 scripts/clarity_audit.py docker --level 1  # Essentials only
    python3 scripts/clarity_audit.py --summary         # whole catalog, counts

For each lesson (in file order) it prints:
- what the learner already knows: prerequisite lessons (transitively, any tool)
  and the tools this tool builds on;
- terms already taught (by `defines` of those lessons);
- glossary terms the lesson uses that aren't taught yet (these get tap/hover
  definitions), grouped by the paragraph where each first appears, with
  OVER LIMIT where a paragraph piles up more than the level allows.

Only terms with a glossary entry are detected. Jargon nobody has added to a
glossary yet is invisible to this script: read the lesson.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.glossary import Glossary, load_shared, paragraph_label  # noqa: E402

TOOLS_DIR = ROOT / "data" / "tools"
LIMITS = {1: 2, 2: 3, 3: 4, 4: 4}
LEVELS = ["", "Essentials", "Practical", "Advanced", "Expert"]


def ancestors(g: Glossary, cid: str) -> list:
    seen, stack, out = set(), list(g.lessons[cid].get("requires") or []), []
    while stack:
        r = stack.pop()
        if r in seen or r not in g.lessons:
            continue
        seen.add(r)
        out.append(r)
        stack.extend(g.lessons[r].get("requires") or [])
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("tool", nargs="?")
    p.add_argument("--level", type=int)
    p.add_argument("--summary", action="store_true")
    a = p.parse_args()
    docs = [json.loads(f.read_text(encoding="utf-8")) for f in sorted(TOOLS_DIR.glob("*.json"))]
    g = Glossary(docs, load_shared())

    if a.summary or not a.tool:
        rows = []
        for d in docs:
            tid = d["tool"]["id"]
            over = 0
            for c in d["concepts"]:
                lim = LIMITS.get(c["level"], 4)
                over += sum(1 for v in g.new_terms_by_paragraph(c["id"]).values() if len(v) > lim)
            rows.append((over, tid, len(d.get("glossary") or []),
                         sum(1 for c in d["concepts"] if c.get("defines"))))
        print(f"{'tool':28} over-limit paragraphs  glossary terms  lessons with defines")
        for over, tid, ng, nd in sorted(rows, reverse=True):
            print(f"{tid:28} {over:>21}  {ng:>14}  {nd:>20}")
        print(f"shared glossary: {len(g.shared)} terms")
        return 0

    doc = next((d for d in docs if d["tool"]["id"] == a.tool), None)
    if not doc:
        print(f"no tool {a.tool}")
        return 1
    tid = a.tool
    req = g.required_tools(tid)
    print(f"# {doc['tool']['name']} ({tid})")
    print("Builds on tools:", ", ".join(f"{t} up to {LEVELS[l]}" for t, l in req) or "none (assume no background)")
    if req:
        print("Terms taught by those tools:", ", ".join(sorted(g.tool_known(tid))) or "-")
    print()
    for c in doc["concepts"]:
        if a.level and c["level"] != a.level:
            continue
        cid = c["id"]
        anc = ancestors(g, cid)
        print(f"## {cid} — {c['name']} [{LEVELS[c['level']]}]")
        print("  builds on:", ", ".join(g.lessons[x]["name"] + (f" ({g.lesson_tool[x]})" if g.lesson_tool[x] != tid else "")
                                         for x in anc) or "nothing in the tree")
        print("  defines:", ", ".join(c.get("defines") or []) or "-")
        known = sorted(g.taught_before(cid) - {str(x).lower() for x in c.get("defines") or []})
        print("  already taught:", ", ".join(known) or "-")
        lim = LIMITS.get(c["level"], 4)
        per = g.new_terms_by_paragraph(cid)
        if not per:
            print("  new terms: none")
        for i, names in sorted(per.items()):
            flag = "  OVER LIMIT" if len(names) > lim else ""
            print(f"  new terms in {paragraph_label(c, i)}: {', '.join(names)}{flag}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
