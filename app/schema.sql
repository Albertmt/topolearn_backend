-- Topolearn concept graph, stored in a single read-only SQLite file.
--
-- Edges point from a concept to something it requires:
--   prerequisites(concept_pk = "Transformer", prereq_pk = "Attention")
-- Integer keys keep the edge table small (two ints per row) so millions of
-- edges stay in the low hundreds of MB.

PRAGMA journal_mode = OFF;
PRAGMA synchronous = OFF;

CREATE TABLE concepts (
    pk          INTEGER PRIMARY KEY,          -- internal, never exposed
    slug        TEXT    NOT NULL UNIQUE,      -- public id, e.g. "linear_regression"
    name        TEXT    NOT NULL,
    name_norm   TEXT    NOT NULL,             -- lower-cased name for exact matches
    type        TEXT,                         -- concept / definition / theorem / algorithm ...
    domain      TEXT,                         -- e.g. deep_learning, probability_statistics
    description TEXT,
    example     TEXT,                         -- worked example / analogy
    wiki_url    TEXT,
    difficulty  REAL,                         -- 0..1
    popularity  REAL    NOT NULL DEFAULT 0,   -- search ranking tie-breaker
    prereq_count INTEGER NOT NULL DEFAULT 0   -- direct prerequisites (denormalised)
);
CREATE INDEX concepts_name_norm ON concepts(name_norm);

CREATE TABLE aliases (
    concept_pk INTEGER NOT NULL REFERENCES concepts(pk),
    alias      TEXT    NOT NULL,
    alias_norm TEXT    NOT NULL,
    PRIMARY KEY (alias_norm, concept_pk)
) WITHOUT ROWID;

CREATE TABLE prerequisites (
    concept_pk INTEGER NOT NULL REFERENCES concepts(pk),
    prereq_pk  INTEGER NOT NULL REFERENCES concepts(pk),
    why        TEXT,                                  -- one sentence: what the prerequisite is used for
    PRIMARY KEY (concept_pk, prereq_pk)
) WITHOUT ROWID;
-- Reverse lookups ("what does this unlock?")
CREATE INDEX prerequisites_reverse ON prerequisites(prereq_pk, concept_pk);

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
) WITHOUT ROWID;
