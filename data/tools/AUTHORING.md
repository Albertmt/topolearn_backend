# Writing a Topolearn curriculum

Topolearn teaches a tool (a language, library, framework or piece of software) as a
**tree of short lessons**. The learner starts at the foundations (lessons with no
prerequisites, drawn at the bottom of the map) and works upward. Every lesson lists the
lessons it directly builds on. The learner picks how deep to go:

| level | name        | what it means for the learner                                   |
|-------|-------------|-----------------------------------------------------------------|
| 1     | Essentials  | can read code and write small programs / do the basic tasks     |
| 2     | Practical   | can use it productively at work, knows the idioms               |
| 3     | Advanced    | knows the less-common features, performance, design trade-offs  |
| 4     | Expert      | internals, runtime/memory model, the edge cases experts know    |

One file per tool: `data/tools/<tool id>.json`. Validate with

    python3 scripts/validate_content.py data/tools/<tool id>.json

and fix every ERROR and every warning you reasonably can.

## File format

```json
{
  "tool": {
    "id": "python",
    "name": "Python",
    "category": "language",
    "tagline": "Readable general-purpose language for scripting, data and the web",
    "description": "Two or three sentences: what it is, what people use it for, why learn it.",
    "icon": "Py",
    "color": "#3776AB",
    "official_docs": "https://docs.python.org/3/",
    "version": "3.13",
    "aliases": ["py", "python3"],
    "requires": []
  },
  "modules": [
    {"id": "basics", "name": "Basics"},
    {"id": "data-structures", "name": "Data structures"}
  ],
  "concepts": [ ...lessons, see below... ]
}
```

- `category`: one of `language`, `library`, `framework`, `tool`, `database`, `runtime`, `cloud`.
- `icon`: a 1–3 character monogram shown in a coloured circle (e.g. `Py`, `JS`, `Go`, `Rs`, `np`).
- `color`: the tool's brand-ish colour as `#RRGGBB`, readable with white text on it.
- `requires`: tool-level prerequisites for libraries/frameworks, e.g. pandas:
  `[{"tool": "python", "level": 1}, {"tool": "numpy", "level": 1}]` = "know Python and NumPy
  essentials first". Languages usually have none.
- `modules`: 5–9 chapters in learning order. Every lesson belongs to one.

## A lesson

```json
{
  "id": "python.list-comprehensions",
  "name": "List comprehensions",
  "module": "data-structures",
  "level": 2,
  "minutes": 10,
  "summary": "Build a new list from any iterable in one readable expression.",
  "why": "Replaces most append-in-a-loop code, and is how idiomatic Python transforms data.",
  "body": "A **list comprehension** builds a list by describing its elements...\n\nThe general shape is `[expression for item in iterable if condition]`...\n\n- The `if` part is optional and filters items.\n- You can nest `for` clauses; they read left to right like nested loops.\n\nUse a comprehension when ...",
  "code": "squares = [n * n for n in range(10)]\nevens = [n for n in range(20) if n % 2 == 0]\n...",
  "code_lang": "python",
  "takeaways": [
    "`[expr for x in items if cond]` = map + filter in one expression",
    "Use a plain loop once it stops fitting comfortably on one or two lines"
  ],
  "pitfalls": ["Using a comprehension only for side effects (like printing) — use a loop."],
  "quiz": [
    {
      "q": "What does [x * 2 for x in range(3) if x != 1] evaluate to?",
      "options": ["[0, 2, 4]", "[0, 4]", "[2, 4]", "[0, 1, 4]"],
      "answer": 1,
      "explain": "range(3) gives 0, 1, 2; the filter drops 1; doubling 0 and 2 gives [0, 4]."
    }
  ],
  "docs": "https://docs.python.org/3/tutorial/datastructures.html#list-comprehensions",
  "requires": ["python.lists", "python.for-loops"]
}
```

Field rules:

- `id`: `<tool id>.<kebab-case-topic>`; stable, unique, never reused.
- `name`: ≤ 48 characters (it is printed on a node of the map). Title case not required.
- `level`: 1–4 as in the table. **A lesson may only require lessons of the same tool with
  the same or a lower level** (an Essentials lesson can't need an Advanced one).
- `minutes`: honest reading + trying-it time, 3–60 (most lessons 5–15).
- `summary`: one sentence, ≤ 160 characters, shown in lists and on the map.
- `why`: one sentence on why it matters / when you need it.
- `body`: the tutorial itself, **120–320 words**, in "markdown-lite":
  - paragraphs separated by a blank line (`\n\n`),
  - `inline code` in backticks, **bold** for the key term (use sparingly),
  - bullet lines starting with `- `,
  - optional fenced code blocks ```` ```python ... ``` ```` for short extra snippets.
  - No headings, tables, links or images. Teach — explain the idea, show how it's used,
    mention the gotcha — as a good senior engineer would to a colleague. Be accurate and
    current for the stated version.
- `code`: one self-contained example (≤ 25 lines, ideally runnable as is) showing the idea
  in practice, with short comments showing output where helpful. `code_lang` is the
  language name in lower case (`python`, `javascript`, `typescript`, `java`, `go`, `rust`,
  `sql`, `cpp`, `bash`, `dockerfile`, `yaml`, `json`, `jsx`, `tsx`...). Omit both only for a
  lesson where code makes no sense.
- `takeaways`: 2–4 short bullet points to remember.
- `pitfalls`: 0–2 common mistakes.
- `quiz`: 1–2 multiple-choice questions that check understanding (not trivia), 3–4 options,
  exactly one correct (`answer` = 0-based index), each with a one-sentence `explain`.
  Vary the position of the correct answer.
- `docs`: a deep link into the **official** documentation for this topic. Only use URLs
  you are confident exist; if unsure, link the closest official page you are sure of.
- `requires`: the **direct** prerequisites only (don't list a prerequisite's prerequisites).
  Lessons of another tool may be listed only if that tool is in `tool.requires` and the
  lesson id exists in its file.

## Shape of a good curriculum

- 30–45 lessons for a language, 18–30 for a library/framework/tool, spread over all four
  levels (roughly 35% Essentials, 30% Practical, 20% Advanced, 15% Expert).
- It is a **tree you climb**: a handful of foundations at the bottom (no `requires`),
  everything else builds on something. Most lessons have 1–3 prerequisites. Avoid long
  single chains; real topics branch and join.
- Order lessons in the file in a sensible study order (foundations first).
- Cover what people actually need, in the order they need it, through to genuinely
  expert material at level 4.
