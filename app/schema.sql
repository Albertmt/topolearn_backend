-- Topolearn concept graph (Postgres). Loaded by scripts/build_db.py into a
-- fresh schema ({schema}) that is then swapped in for "graph" in one step,
-- so the API never sees a half-loaded graph.
--
-- Edges point from a concept to something it requires:
--   prerequisites(concept_pk = "Transformer", prereq_pk = "Attention")
-- Integer keys keep the edge table small; pks are assigned in popularity
-- order (1 = most popular), which search uses as its ranking tie-breaker.

CREATE TABLE {schema}.concepts (
    pk           integer PRIMARY KEY,               -- internal, never exposed
    slug         text    NOT NULL,                  -- public id, e.g. "linear_regression"
    name         text    NOT NULL,
    name_norm    text    NOT NULL,                  -- lower-cased, single-spaced name (exact matches)
    name_key     text    NOT NULL,                  -- accent-free words of the name ("starts with")
    search_doc   text    NOT NULL,                  -- accent-free words of name + aliases (full text)
    type         text,                              -- concept / definition / theorem / algorithm ...
    domain       text,                              -- e.g. deep_learning, probability_statistics
    description  text,
    example      text,                              -- worked example / analogy
    wiki_url     text,
    difficulty   double precision,                  -- 0..1
    popularity   double precision NOT NULL DEFAULT 0,
    prereq_count integer NOT NULL DEFAULT 0         -- direct prerequisites (denormalised)
);

CREATE TABLE {schema}.aliases (
    concept_pk integer NOT NULL,
    alias      text    NOT NULL,
    alias_norm text    NOT NULL
);

CREATE TABLE {schema}.prerequisites (
    concept_pk integer NOT NULL,
    prereq_pk  integer NOT NULL,
    why        text                                 -- one sentence: what the prerequisite is used for
);

CREATE TABLE {schema}.meta (
    key   text PRIMARY KEY,
    value text
);

-- @after-load: constraints and indexes are added once the rows are in (much faster).
ALTER TABLE {schema}.concepts ADD CONSTRAINT concepts_slug_key UNIQUE (slug);
ALTER TABLE {schema}.aliases ADD PRIMARY KEY (alias_norm, concept_pk);
ALTER TABLE {schema}.aliases ADD FOREIGN KEY (concept_pk) REFERENCES {schema}.concepts(pk);
ALTER TABLE {schema}.prerequisites ADD PRIMARY KEY (concept_pk, prereq_pk);
ALTER TABLE {schema}.prerequisites ADD FOREIGN KEY (concept_pk) REFERENCES {schema}.concepts(pk);
ALTER TABLE {schema}.prerequisites ADD FOREIGN KEY (prereq_pk) REFERENCES {schema}.concepts(pk);
-- "What does this unlock?"
CREATE INDEX ON {schema}.prerequisites (prereq_pk, concept_pk);
CREATE INDEX ON {schema}.concepts (name_norm);
CREATE INDEX ON {schema}.concepts (name_key text_pattern_ops, pk);
-- Full-text search (an expression index, so the vectors aren't stored twice).
CREATE INDEX concepts_search_idx ON {schema}.concepts USING gin (to_tsvector('simple', search_doc));
