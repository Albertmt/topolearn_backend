"""The writable user-data database (users, their progress, learning sources).

Kept apart from the read-only concept DB. Tables are created on first use and
new tables are added automatically to an existing file (CREATE ... IF NOT EXISTS).
"""
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator, Optional

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    email           TEXT UNIQUE,
    auth_provider   TEXT NOT NULL DEFAULT 'dummy',
    auth_subject    TEXT,
    birth_year      INTEGER,
    education_level TEXT,
    interests       TEXT NOT NULL DEFAULT '[]',   -- JSON list of concept ids
    onboarded       INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);

-- How well a user knows a concept, and whether they flagged it.
--   understanding: 0..100, NULL = not rated
--   status: NULL | 'need_to_learn' | 'hidden'
CREATE TABLE IF NOT EXISTS concept_progress (
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    concept_id    TEXT NOT NULL,
    understanding INTEGER CHECK (understanding BETWEEN 0 AND 100),
    status        TEXT CHECK (status IN ('need_to_learn', 'hidden')),
    updated_at    REAL NOT NULL,
    PRIMARY KEY (user_id, concept_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS concept_progress_status ON concept_progress(user_id, status);

-- Community "where to learn this" links, shared by all users.
CREATE TABLE IF NOT EXISTS sources (
    id          TEXT PRIMARY KEY,
    concept_id  TEXT NOT NULL,
    url         TEXT NOT NULL,
    title       TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'other',
    added_by    TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at  REAL NOT NULL,
    UNIQUE (concept_id, url)
);
CREATE INDEX IF NOT EXISTS sources_concept ON sources(concept_id);

-- A source can teach several concepts (the one it was added on, plus "also covers").
CREATE TABLE IF NOT EXISTS source_concepts (
    source_id  TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    concept_id TEXT NOT NULL,
    PRIMARY KEY (source_id, concept_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS source_concepts_concept ON source_concepts(concept_id);
-- Sources added before "also covers" existed.
INSERT OR IGNORE INTO source_concepts (source_id, concept_id) SELECT id, concept_id FROM sources;

-- "This helped me understand it" — a stronger signal than a vote.
CREATE TABLE IF NOT EXISTS source_helped (
    source_id  TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    PRIMARY KEY (source_id, user_id)
) WITHOUT ROWID;

-- Concepts a learner is working towards.
CREATE TABLE IF NOT EXISTS user_goals (
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    concept_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (user_id, concept_id)
) WITHOUT ROWID;

-- Every progress change, for streaks and "mastered this week".
CREATE TABLE IF NOT EXISTS progress_events (
    id            INTEGER PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    concept_id    TEXT NOT NULL,
    understanding INTEGER,
    status        TEXT,
    at            REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS progress_events_user ON progress_events(user_id, at);

-- "Something is wrong here" reports from learners, for review.
CREATE TABLE IF NOT EXISTS reports (
    id                 TEXT PRIMARY KEY,
    user_id            TEXT REFERENCES users(id) ON DELETE SET NULL,
    kind               TEXT NOT NULL,
    concept_id         TEXT NOT NULL,
    related_concept_id TEXT,
    context_concept_id TEXT,
    message            TEXT NOT NULL DEFAULT '',
    suggestion         TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL DEFAULT 'open',
    created_at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS reports_status ON reports(status, created_at);

CREATE TABLE IF NOT EXISTS source_votes (
    source_id  TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    value      INTEGER NOT NULL CHECK (value IN (-1, 1)),
    PRIMARY KEY (source_id, user_id)
) WITHOUT ROWID;
"""

_init_lock = threading.Lock()
_initialised_for: Optional[str] = None


def connect() -> sqlite3.Connection:
    global _initialised_for
    path = config.USERS_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if _initialised_for != str(path):
        with _init_lock:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            _initialised_for = str(path)
    return conn


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    """Commit on success, roll back on error, always close."""
    conn = connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def user_exists(conn: sqlite3.Connection, user_id: str) -> bool:
    return conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is not None
