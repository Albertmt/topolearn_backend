"""Glossary: which terms a lesson uses that the learner hasn't been taught yet.

Definitions live in two places:
- `data/glossary.json`: general computing terms shared by every tool
  ("kernel", "API", "environment variable").
- a tool file's `glossary`: terms specific to that tool ("pod", "DataFrame").

A lesson lists the terms it teaches in `defines`. By the time a learner opens a
lesson they know every term defined by the lessons it builds on: its
prerequisites (transitively, across tools) and the lessons of the tools its
tool requires, up to the declared level.

For each lesson, `lesson_glossary` returns the terms it uses that aren't taught
by the lesson or what it builds on. The app and website underline the first use
and show the definition on tap/hover; the validator uses the same data to stop
Essentials lessons from piling unexplained terms on a newcomer.

Pure Python, no database: used by scripts/build_db.py, scripts/validate_content.py
and scripts/clarity_audit.py.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

GLOSSARY_FILE = Path(
    __import__("os").environ.get("TOPOLEARN_GLOSSARY")
    or Path(__file__).resolve().parent.parent / "data" / "glossary.json"
)

# Essentials: at most this many not-yet-taught terms in one paragraph.
MAX_NEW_TERMS_PER_PARAGRAPH = 2


_ACRONYM = re.compile(r"[A-Z][A-Z0-9]+s?")


def form_pattern(form: str, case: bool) -> Tuple[str, bool]:
    """Regex source for one spelling of a term, and whether it is matched
    case-sensitively. Shared rules (the app and website do the same):
    - acronyms ("VM", "GUID", "VMs"), spellings with capitals inside a word
      ("PoP", "DataFrame") and terms marked `case` match their case;
      they get an optional plain "s" plural ("VMs", "Dockerfiles");
    - everything else is case-insensitive with an optional "s"/"es" plural;
    - spaces also match hyphens and line breaks ("command line" = "command-line")."""
    # Capitals inside a word ("PoP", "macOS", "DataFrame") also mean "match my case".
    cs = case or bool(_ACRONYM.fullmatch(form)) or bool(re.search(r"\w[A-Z]", form))
    esc = re.escape(form).replace(r"\ ", r"[\s-]+")
    if re.search(r"[A-Za-z]$", form) and not form.endswith("s"):
        esc += r"s?" if cs else r"(?:e?s)?"
    elif not cs and re.search(r"[a-z]$", form):
        esc += r"(?:es)?"
    return esc, cs


def _norm(s: str) -> str:
    return re.sub(r"[\s-]+", " ", s.lower())


class Term:
    __slots__ = ("term", "aliases", "definition", "case", "tool", "pattern", "forms")

    def __init__(self, raw: dict, tool: Optional[str]):
        self.term: str = str(raw["term"]).strip()
        self.aliases: List[str] = [str(a).strip() for a in raw.get("aliases") or [] if str(a).strip()]
        self.definition: str = str(raw.get("def", "")).strip()
        self.case: bool = bool(raw.get("case", False))
        self.tool: Optional[str] = tool  # None: shared glossary
        forms = sorted({self.term, *self.aliases}, key=len, reverse=True)
        self.forms = []
        alts = []
        for f in forms:
            src, cs = form_pattern(f, self.case)
            self.forms.append(re.compile(src, 0 if cs else re.IGNORECASE))
            alts.append(f"(?-i:{src})" if cs else src)
        # Whole words only. \b fails next to "+"/"#" (C++, C#), hence lookarounds.
        self.pattern = re.compile(rf"(?<![\w-])(?:{'|'.join(alts)})(?![\w])", re.IGNORECASE)

    def matches(self, word: str) -> bool:
        return any(f.fullmatch(word) for f in self.forms)

    @property
    def key(self) -> str:
        return self.term.lower()

    def as_api(self, lesson: Optional[str] = None) -> dict:
        out = {"term": self.term, "aliases": self.aliases, "def": self.definition}
        if self.case:
            out["case"] = True
        if lesson:
            out["lesson"] = lesson
        return out


_FENCE = re.compile(r"```.*?```", re.S)
_CODE = re.compile(r"`[^`\n]*`")
_LINK_URL = re.compile(r"\]\([^)]*\)")


def prose(text: str) -> str:
    """Lesson markdown without code (blocks and inline) or link targets: terms
    inside code are code, not jargon to explain."""
    text = _FENCE.sub(" ", text or "")
    text = _CODE.sub(" ", text)
    return _LINK_URL.sub("]", text)


def lesson_texts(c: dict) -> List[str]:
    """The prose a learner reads, in reading order (body paragraphs first)."""
    parts = [c.get("summary") or "", c.get("why") or ""]
    parts += [p for p in re.split(r"\n\s*\n", c.get("body") or "")]
    parts += [str(x) for x in c.get("takeaways") or []]
    parts += [str(x) for x in c.get("pitfalls") or []]
    return [prose(p) for p in parts if p and p.strip()]


def load_shared(path: Path = GLOSSARY_FILE) -> List[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("terms", [])


class Glossary:
    """All terms, plus which tools can see which tool-specific terms."""

    def __init__(self, docs: Iterable[dict], shared: Optional[List[dict]] = None):
        docs = list(docs)
        self.shared: Dict[str, Term] = {}
        for raw in shared if shared is not None else load_shared():
            t = Term(raw, None)
            self.shared[t.key] = t
        self.by_tool: Dict[str, Dict[str, Term]] = {}
        self.tool_requires: Dict[str, List[Tuple[str, int]]] = {}
        self.tool_names: Dict[str, Set[str]] = {}
        self.lessons: Dict[str, dict] = {}
        self.lesson_tool: Dict[str, str] = {}
        for doc in docs:
            tid = doc["tool"]["id"]
            self.by_tool[tid] = {}
            for raw in doc.get("glossary") or []:
                t = Term(raw, tid)
                self.by_tool[tid][t.key] = t
            info = doc["tool"]
            # Only the name itself: `aliases` are search keywords ("containers"
            # for Docker) and mustn't count as taught.
            names = {str(info.get("name", "")).lower(), tid.lower()}
            self.tool_names[tid] = {n for n in names if n} | {
                t.key for t in self.shared.values() if names & {t.key, *(a.lower() for a in t.aliases)}
            }
            self.tool_requires[tid] = [
                (r["tool"], int(r.get("level", 1))) for r in doc["tool"].get("requires") or [] if r.get("tool")
            ]
            for c in doc.get("concepts", []):
                self.lessons[c["id"]] = c
                self.lesson_tool[c["id"]] = tid
        self._visible: Dict[str, Dict[str, Term]] = {}
        self._known: Dict[str, Set[str]] = {}
        self._definer: Dict[Tuple[str, str], str] = {}
        # Which lesson teaches a term, per tool (first in file order).
        for cid, c in self.lessons.items():
            for d in c.get("defines") or []:
                self._definer.setdefault((self.lesson_tool[cid], str(d).lower()), cid)

    # -- which terms a tool's lessons may use -------------------------------
    def required_tools(self, tid: str) -> List[Tuple[str, int]]:
        """Tools `tid` builds on, transitively, with the level learned."""
        out: Dict[str, int] = {}
        stack = list(self.tool_requires.get(tid, []))
        while stack:
            t, lvl = stack.pop()
            if t == tid or out.get(t, 0) >= lvl:
                continue
            out[t] = max(out.get(t, 0), lvl)
            stack.extend(self.tool_requires.get(t, []))
        return sorted(out.items())

    def visible(self, tid: str) -> Dict[str, Term]:
        """Terms with a definition a lesson of `tid` can show: shared terms, the
        tool's own, and those of the tools it builds on (own tool wins)."""
        if tid not in self._visible:
            v = dict(self.shared)
            for t, _ in self.required_tools(tid):
                v.update(self.by_tool.get(t, {}))
            v.update(self.by_tool.get(tid, {}))
            self._visible[tid] = v
        return self._visible[tid]

    def lookup(self, tid: str, name: str) -> Optional[Term]:
        return self.visible(tid).get(name.lower())

    # -- what the learner already knows ---------------------------------------
    def known_terms(self, cid: str) -> Set[str]:
        """Terms taught by the lesson itself and everything it builds on."""
        if cid in self._known:
            return self._known[cid]
        self._known[cid] = set()  # guards against loops in bad data
        c = self.lessons[cid]
        tid = self.lesson_tool[cid]
        known = {str(d).lower() for d in c.get("defines") or []}
        for r in c.get("requires") or []:
            if r in self.lessons:
                known |= self.known_terms(r)
        self._known[cid] = known
        return known

    def tool_known(self, tid: str) -> Set[str]:
        """Terms taught by the required tools' lessons up to the declared level,
        plus the names of the tool itself and the tools it builds on: a
        Kubernetes lesson needn't define "Kubernetes"."""
        key = "@" + tid
        if key not in self._known:
            known: Set[str] = set()
            for t in [tid] + [x for x, _ in self.required_tools(tid)]:
                known |= self.tool_names.get(t, set())
            for t, lvl in self.required_tools(tid):
                for cid, c in self.lessons.items():
                    if self.lesson_tool[cid] == t and c.get("level", 1) <= lvl:
                        known |= {str(d).lower() for d in c.get("defines") or []}
            self._known[key] = known
        return self._known[key]

    def taught_before(self, cid: str) -> Set[str]:
        return self.known_terms(cid) | self.tool_known(self.lesson_tool[cid])

    # -- per lesson ----------------------------------------------------------
    def _scanner(self, terms: Dict[str, Term]):
        """One combined regex for a set of terms (longest forms first)."""
        key = id(terms)
        cache = self.__dict__.setdefault("_scanners", {})
        if key not in cache or cache[key][2] is not terms:  # ids get reused
            forms = []
            for k, t in terms.items():
                for f in {t.term, *t.aliases}:
                    forms.append((f, k, t))
            # Longest first; on ties a tool's own term beats a shared one.
            forms.sort(key=lambda x: (-len(x[0]), x[2].tool is None))
            alts = []
            for f, _, t in forms:
                src, cs = form_pattern(f, t.case)
                alts.append(f"(?-i:{src})" if cs else src)
            rx = re.compile(rf"(?<![\w-])(?:{'|'.join(alts)})(?![\w])", re.IGNORECASE) if alts else None
            # Matched text -> candidate terms, by lower-cased spelling
            # (longest forms first, so the owning term comes first).
            owners: Dict[str, List[Tuple[str, Term]]] = {}
            for f, k, t in forms:
                owners.setdefault(_norm(f), []).append((k, t))
            cache[key] = (rx, owners, terms)
        return cache[key]

    def scan(self, text: str, terms: Dict[str, Term]) -> List[Tuple[int, int, str]]:
        """(start, end, term key) of every term in `text`, non-overlapping,
        longest first."""
        rx, owners, _ = self._scanner(terms)
        if rx is None:
            return []
        out = []
        for m in rx.finditer(text):
            word = m.group(0)
            n = _norm(word)
            cands = owners.get(n, []) + owners.get(n[:-1], []) + owners.get(n[:-2], [])
            for k, t in cands:
                if t.matches(word):
                    out.append((m.start(), m.end(), k))
                    break
        return out

    def terms_in(self, cid: str) -> List[Tuple[Term, List[int]]]:
        """Visible terms the lesson's prose uses, with the indexes of the
        paragraphs (lesson_texts) they appear in, in order of first use.
        Overlapping matches go to the longest term ("virtual machine" beats
        "machine")."""
        c = self.lessons[cid]
        tid = self.lesson_tool[cid]
        terms = self.visible(tid)
        found: Dict[str, List[int]] = {}
        for i, text in enumerate(lesson_texts(c)):
            for _, _, key in self.scan(text, terms):
                paras = found.setdefault(key, [])
                if i not in paras:
                    paras.append(i)
        return [(terms[k], sorted(v)) for k, v in found.items()]

    def definer(self, term: Term, cid: str) -> Optional[str]:
        """A lesson that teaches `term` (own tool first), for "learn more"."""
        tid = self.lesson_tool[cid]
        for t in [tid] + [x for x, _ in self.required_tools(tid)] + ([term.tool] if term.tool else []):
            hit = self._definer.get((t, term.key))
            if hit and hit != cid:
                return hit
        return None

    def lesson_glossary(self, cid: str) -> List[dict]:
        """Terms the lesson uses that the learner hasn't been taught: shown as
        tap/hover definitions. Terms the lesson itself teaches are left out."""
        known = self.taught_before(cid)
        out = []
        for term, _ in self.terms_in(cid):
            if term.key in known:
                continue
            entry = term.as_api(self.definer(term, cid))
            if "lesson" in entry:
                entry["lesson_name"] = self.lessons[entry["lesson"]].get("name", "")
            out.append(entry)
        return out

    def new_terms_by_paragraph(self, cid: str) -> Dict[int, List[str]]:
        """Not-yet-taught terms, under the paragraph (index into lesson_texts)
        where each is first used."""
        known = self.taught_before(cid)
        per: Dict[int, List[str]] = defaultdict(list)
        for term, paras in self.terms_in(cid):
            if term.key not in known:
                per[paras[0]].append(term.term)
        return per


def paragraph_label(c: dict, i: int) -> str:
    """Human label for lesson_texts index `i`."""
    n_body = len([p for p in re.split(r"\n\s*\n", c.get("body") or "") if p.strip()])
    head = [x for x in ("summary", "why") if c.get(x)]
    if i < len(head):
        return head[i]
    i -= len(head)
    if i < n_body:
        return f"body paragraph {i + 1}"
    i -= n_body
    n_take = len(c.get("takeaways") or [])
    return f"takeaway {i + 1}" if i < n_take else f"pitfall {i - n_take + 1}"
