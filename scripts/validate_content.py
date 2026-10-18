#!/usr/bin/env python3
"""Check the tool curricula in data/tools/*.json before loading them.

    python scripts/validate_content.py                 # every file in data/tools
    python scripts/validate_content.py data/tools/python.json --strict

Errors (exit 1): broken structure, unknown ids, cycles, a lesson that needs a
deeper lesson of the same tool, bad quiz answers. Warnings: style and length
hints (with --strict they count as errors). Cross-tool prerequisites pointing
at a tool file that doesn't exist yet are reported but not fatal.
"""
import argparse
import json
import re
import sys
from collections import defaultdict, deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = ROOT / "data" / "tools"

CATEGORIES = {"language", "library", "framework", "tool", "database", "runtime", "cloud"}
LEVELS = {1: "Essentials", 2: "Practical", 3: "Advanced", 4: "Expert"}
TOOL_ID = re.compile(r"^[a-z][a-z0-9-]*$")
CONCEPT_ID = re.compile(r"^[a-z][a-z0-9-]*\.[a-z0-9][a-z0-9-]*$")
COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


class Report:
    def __init__(self) -> None:
        self.errors: list = []
        self.warnings: list = []

    def err(self, where: str, msg: str) -> None:
        self.errors.append(f"{where}: {msg}")

    def warn(self, where: str, msg: str) -> None:
        self.warnings.append(f"{where}: {msg}")


def _words(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


def check_file(path: Path, data: dict, rep: Report) -> None:
    where = path.name
    tool = data.get("tool")
    if not isinstance(tool, dict):
        rep.err(where, 'missing "tool" object')
        return
    tid = tool.get("id", "")
    if not TOOL_ID.match(str(tid)):
        rep.err(where, f"bad tool id {tid!r}")
    if path.stem != tid:
        rep.err(where, f"file name must be <tool id>.json ({tid}.json)")
    for key in ("name", "tagline", "description", "icon", "color", "official_docs", "category"):
        if not str(tool.get(key, "")).strip():
            rep.err(where, f"tool.{key} is required")
    if tool.get("category") not in CATEGORIES:
        rep.err(where, f"tool.category must be one of {sorted(CATEGORIES)}")
    if tool.get("color") and not COLOR.match(tool["color"]):
        rep.err(where, "tool.color must look like #3776AB")
    if len(str(tool.get("icon", ""))) > 3:
        rep.err(where, "tool.icon is a 1-3 character monogram, e.g. Py")
    if len(str(tool.get("tagline", ""))) > 90:
        rep.warn(where, "tool.tagline is longer than 90 characters")
    for r in tool.get("requires", []) or []:
        if not isinstance(r, dict) or not TOOL_ID.match(str(r.get("tool", ""))) or r.get("level") not in LEVELS:
            rep.err(where, f'tool.requires entries look like {{"tool": "python", "level": 1}}, got {r!r}')

    modules = data.get("modules") or []
    module_ids = [m.get("id") for m in modules if isinstance(m, dict)]
    if not modules or len(module_ids) != len(set(module_ids)):
        rep.err(where, "modules must be a non-empty list with unique ids")
    for m in modules:
        if not str(m.get("name", "")).strip():
            rep.err(where, f"module {m.get('id')} needs a name")

    concepts = data.get("concepts") or []
    if len(concepts) < 8:
        rep.err(where, f"only {len(concepts)} concepts")
    seen = set()
    for c in concepts:
        cid = str(c.get("id", ""))
        cw = f"{where} {cid or '?'}"
        if not CONCEPT_ID.match(cid) or not cid.startswith(f"{tid}."):
            rep.err(cw, f'id must look like "{tid}.some-topic"')
        if cid in seen:
            rep.err(cw, "duplicate id")
        seen.add(cid)
        for key in ("name", "summary", "why", "body", "module"):
            if not str(c.get(key, "")).strip():
                rep.err(cw, f"{key} is required")
        if c.get("module") not in module_ids:
            rep.err(cw, f"unknown module {c.get('module')!r}")
        if c.get("level") not in LEVELS:
            rep.err(cw, "level must be 1, 2, 3 or 4")
        if not isinstance(c.get("minutes"), int) or not 3 <= c["minutes"] <= 60:
            rep.err(cw, "minutes must be an integer 3..60")
        if len(str(c.get("name", ""))) > 48:
            rep.warn(cw, "name longer than 48 characters (it must fit on a map node)")
        if len(str(c.get("summary", ""))) > 160:
            rep.warn(cw, "summary longer than 160 characters")
        n = _words(c.get("body", ""))
        if n < 90:
            rep.warn(cw, f"body is short ({n} words, aim for 120-320)")
        if n > 450:
            rep.warn(cw, f"body is long ({n} words, aim for 120-320)")
        if str(c.get("body", "")).count("```") % 2:
            rep.err(cw, "unbalanced ``` fence in body")
        code = c.get("code")
        if code is not None:
            if not isinstance(code, str) or not code.strip():
                rep.err(cw, "code must be a non-empty string (or omitted)")
            elif code.count("\n") > 40:
                rep.warn(cw, "code example longer than 40 lines")
            if not c.get("code_lang"):
                rep.err(cw, "code_lang is required when code is given")
        tk = c.get("takeaways")
        if not isinstance(tk, list) or not 2 <= len(tk) <= 5 or not all(isinstance(t, str) and t.strip() for t in tk):
            rep.err(cw, "takeaways must be 2-5 strings")
        pf = c.get("pitfalls", [])
        if not isinstance(pf, list) or len(pf) > 3 or not all(isinstance(t, str) and t.strip() for t in pf):
            rep.err(cw, "pitfalls must be 0-3 strings")
        quiz = c.get("quiz")
        if not isinstance(quiz, list) or not 1 <= len(quiz) <= 3:
            rep.err(cw, "quiz must have 1-3 questions")
        else:
            for i, q in enumerate(quiz):
                opts = q.get("options")
                if not str(q.get("q", "")).strip() or not isinstance(opts, list) or not 2 <= len(opts) <= 5:
                    rep.err(cw, f"quiz[{i}] needs q and 2-5 options")
                    continue
                if len(set(map(str, opts))) != len(opts):
                    rep.err(cw, f"quiz[{i}] has duplicate options")
                if not isinstance(q.get("answer"), int) or not 0 <= q["answer"] < len(opts):
                    rep.err(cw, f"quiz[{i}].answer must be an index into options")
                if not str(q.get("explain", "")).strip():
                    rep.err(cw, f"quiz[{i}].explain is required")
        docs = c.get("docs")
        if docs is not None and not str(docs).startswith("https://"):
            rep.err(cw, "docs must be an https:// link (or omitted)")
        req = c.get("requires", [])
        if not isinstance(req, list) or not all(isinstance(r, str) for r in req):
            rep.err(cw, "requires must be a list of concept ids")
        elif len(set(req)) != len(req):
            rep.err(cw, "requires has duplicates")


def check_graph(files: dict, rep: Report, all_known: dict) -> None:
    """all_known: concept id -> (tool id, level) for every file on disk."""
    tools_on_disk = {cid.split(".")[0] for cid in all_known}
    for path, data in files.items():
        tid = data["tool"]["id"]
        declared = {r["tool"] for r in data["tool"].get("requires", []) or [] if isinstance(r, dict)}
        out = defaultdict(list)
        for c in data.get("concepts", []):
            cid = c.get("id")
            for r in c.get("requires", []) or []:
                rt = r.split(".")[0]
                if r == cid:
                    rep.err(f"{path.name} {cid}", "requires itself")
                elif r in all_known:
                    if rt == tid:
                        out[cid].append(r)
                        if all_known[r][1] > c.get("level", 0):
                            rep.err(f"{path.name} {cid}",
                                    f"level {c.get('level')} lesson requires deeper lesson {r} (level {all_known[r][1]})")
                    elif rt not in declared:
                        rep.err(f"{path.name} {cid}", f'requires {r} but tool.requires does not list "{rt}"')
                elif rt == tid or rt in tools_on_disk:
                    rep.err(f"{path.name} {cid}", f"requires unknown concept {r}")
                else:
                    rep.warn(f"{path.name} {cid}", f"requires {r} from {rt}.json, which doesn't exist yet")
        # cycles within the tool (cross-tool cycles are checked by the loader)
        ids = [c.get("id") for c in data.get("concepts", [])]
        indeg = {i: 0 for i in ids}
        for a, bs in out.items():
            for b in bs:
                indeg[b] = indeg.get(b, 0) + 1
        q = deque(i for i, d in indeg.items() if d == 0)
        n = 0
        while q:
            x = q.popleft()
            n += 1
            for b in out.get(x, []):
                indeg[b] -= 1
                if indeg[b] == 0:
                    q.append(b)
        if n != len(indeg):
            rep.err(path.name, f"prerequisite cycle among {[i for i, d in indeg.items() if d > 0][:8]}")
        # every lesson but the first ones should build on something
        roots = [c["id"] for c in data.get("concepts", []) if not c.get("requires")]
        if len(roots) > max(3, len(ids) // 6):
            rep.warn(path.name, f"{len(roots)} lessons have no prerequisites; link them into the tree")
        levels = defaultdict(int)
        for c in data.get("concepts", []):
            levels[c.get("level")] += 1
        for lv in LEVELS:
            if levels[lv] < 3:
                rep.warn(path.name, f"only {levels[lv]} lessons at level {lv} ({LEVELS[lv]})")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="*", type=Path)
    p.add_argument("--strict", action="store_true", help="treat warnings as errors")
    args = p.parse_args()
    every = sorted(TOOLS_DIR.glob("*.json"))
    targets = [f.resolve() for f in args.files] or every
    rep = Report()
    loaded = {}
    for f in sorted(set(every) | set(targets)):
        try:
            loaded[f.resolve()] = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            rep.err(f.name, f"not valid JSON: {e}")
    known = {}
    for f, d in loaded.items():
        for c in d.get("concepts", []) or []:
            if isinstance(c, dict) and c.get("id"):
                known[c["id"]] = (str(c["id"]).split(".")[0], c.get("level") or 0)
    files = {f: loaded[f] for f in targets if f in loaded}
    for f, d in files.items():
        check_file(f, d, rep)
    if not rep.errors:
        check_graph(files, rep, known)
    for w in rep.warnings:
        print("warning:", w)
    for e in rep.errors:
        print("ERROR:", e)
    total = sum(len(d.get("concepts", [])) for d in files.values())
    bad = rep.errors or (args.strict and rep.warnings)
    print(f"{'FAILED' if bad else 'OK'}: {len(files)} file(s), {total} lessons, "
          f"{len(rep.errors)} errors, {len(rep.warnings)} warnings")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
