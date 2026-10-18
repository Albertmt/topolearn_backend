-- Topolearn catalog (Postgres): tools and the lessons that teach them.
-- Loaded by scripts/build_db.py from data/tools/*.json into a fresh schema
-- ({schema}) that is then swapped in for "graph" in one step, so the API never
-- sees a half-loaded catalog.
--
-- A tool (Python, pandas, Git...) is taught as a tree of lessons ("concepts").
-- Edges point from a lesson to a lesson it requires, possibly of another tool:
--   prerequisites(concept_pk = "pandas.dataframes", prereq_pk = "python.dicts")
-- Every lesson has a depth level: 1 Essentials, 2 Practical, 3 Advanced, 4 Expert.
-- Integer keys keep the edge table small; they are never exposed.

CREATE TABLE {schema}.tools (
    pk            integer PRIMARY KEY,
    slug          text    NOT NULL,                 -- public id, e.g. "python"
    name          text    NOT NULL,
    name_norm     text    NOT NULL,                 -- lower-cased, single-spaced
    search_doc    text    NOT NULL,                 -- accent-free words of name + aliases
    category      text    NOT NULL,                 -- language / library / framework / tool / runtime / database
    tagline       text    NOT NULL DEFAULT '',
    description   text    NOT NULL DEFAULT '',
    icon          text    NOT NULL DEFAULT '',      -- 1-3 character monogram
    color         text    NOT NULL DEFAULT '#4F46E5',
    official_docs text,
    version       text,
    position      integer NOT NULL,                 -- catalog order (most important first)
    modules       jsonb   NOT NULL DEFAULT '[]',    -- [{"id", "name"}] in learning order
    logo_svg      text                               -- 24x24 single-path SVG (data/logos), or NULL
);

CREATE TABLE {schema}.tool_aliases (
    tool_pk    integer NOT NULL,
    alias      text    NOT NULL,
    alias_norm text    NOT NULL
);

-- "Learn Python (Essentials) before pandas."
CREATE TABLE {schema}.tool_requires (
    tool_pk     integer  NOT NULL,
    requires_pk integer  NOT NULL,
    level       smallint NOT NULL
);

CREATE TABLE {schema}.concepts (
    pk           integer  PRIMARY KEY,
    slug         text     NOT NULL,                 -- public id, e.g. "python.list-comprehensions"
    tool_pk      integer  NOT NULL,
    module       text     NOT NULL,
    level        smallint NOT NULL,                 -- 1..4
    position     integer  NOT NULL,                 -- study order within the tool
    minutes      integer  NOT NULL,
    name         text     NOT NULL,
    name_norm    text     NOT NULL,
    name_key     text     NOT NULL,                 -- accent-free words of the name ("starts with")
    search_doc   text     NOT NULL,                 -- accent-free words of name + summary (full text)
    summary      text     NOT NULL DEFAULT '',
    why          text     NOT NULL DEFAULT '',
    body         text     NOT NULL DEFAULT '',      -- markdown-lite tutorial
    code         text,
    code_lang    text,
    takeaways    jsonb    NOT NULL DEFAULT '[]',
    pitfalls     jsonb    NOT NULL DEFAULT '[]',
    quiz         jsonb    NOT NULL DEFAULT '[]',    -- [{"q", "options", "answer", "explain"}]
    docs_url     text,
    prereq_count integer  NOT NULL DEFAULT 0
);

CREATE TABLE {schema}.prerequisites (
    concept_pk integer NOT NULL,
    prereq_pk  integer NOT NULL
);

CREATE TABLE {schema}.meta (
    key   text PRIMARY KEY,
    value text
);

-- @after-load: constraints and indexes are added once the rows are in (much faster).
ALTER TABLE {schema}.tools ADD CONSTRAINT tools_slug_key UNIQUE (slug);
ALTER TABLE {schema}.tool_aliases ADD PRIMARY KEY (alias_norm, tool_pk);
ALTER TABLE {schema}.tool_aliases ADD FOREIGN KEY (tool_pk) REFERENCES {schema}.tools(pk);
ALTER TABLE {schema}.tool_requires ADD PRIMARY KEY (tool_pk, requires_pk);
ALTER TABLE {schema}.concepts ADD CONSTRAINT concepts_slug_key UNIQUE (slug);
ALTER TABLE {schema}.concepts ADD FOREIGN KEY (tool_pk) REFERENCES {schema}.tools(pk);
ALTER TABLE {schema}.prerequisites ADD PRIMARY KEY (concept_pk, prereq_pk);
ALTER TABLE {schema}.prerequisites ADD FOREIGN KEY (concept_pk) REFERENCES {schema}.concepts(pk);
ALTER TABLE {schema}.prerequisites ADD FOREIGN KEY (prereq_pk) REFERENCES {schema}.concepts(pk);
-- "What does this unlock?"
CREATE INDEX ON {schema}.prerequisites (prereq_pk, concept_pk);
CREATE INDEX ON {schema}.concepts (tool_pk, level, position);
CREATE INDEX ON {schema}.concepts (name_norm);
CREATE INDEX ON {schema}.concepts (name_key text_pattern_ops, pk);
CREATE INDEX ON {schema}.tools (name_norm);
-- Full-text search (expression indexes, so the vectors aren't stored twice).
CREATE INDEX concepts_search_idx ON {schema}.concepts USING gin (to_tsvector('simple', search_doc));
CREATE INDEX tools_search_idx ON {schema}.tools USING gin (to_tsvector('simple', search_doc));
