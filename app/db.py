"""Postgres access (Neon in production).

Each server thread keeps one connection, opened on first use and reopened if
it broke or went stale (Neon suspends idle databases after 5 minutes).
Connections run in autocommit mode, so plain reads need no transaction;
writes go through `transaction()`, which nests safely (inner blocks become
savepoints), so helpers can open one inside another.

Neon's pooled endpoint runs PgBouncer in transaction mode, which means:
no server-side prepared statements and no session settings such as
search_path. Concept tables are therefore always written schema-qualified
(graph.concepts, ...).

The SQL in this codebase uses `?` and `:name` placeholders (the DB-API
"qmark"/"named" styles); they are translated to psycopg's `%s` / `%(name)s`.
"""
import re
import threading
import time
from contextlib import ExitStack, contextmanager
from functools import lru_cache
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Union

import psycopg
from psycopg import pq

from . import config

GRAPH = "graph"  # schema holding the concept tables

# Constraint violations (duplicate email, unknown user, ...), for `except db.IntegrityError`.
IntegrityError = psycopg.IntegrityError
Error = psycopg.Error


class DatabaseMissing(RuntimeError):
    """The database isn't configured or the concept data hasn't been loaded."""


# ------------------------------------------------------------------ rows


class Row(tuple):
    """A result row usable by position (row[0]), by column name (row["name"])
    and as a mapping (dict(row)), like sqlite3.Row."""

    def __new__(cls, values: Sequence[Any], names: List[str], index: Dict[str, int]):
        row = super().__new__(cls, values)
        row._names = names
        row._index = index
        return row

    def __getitem__(self, key):  # type: ignore[override]
        if isinstance(key, str):
            return tuple.__getitem__(self, self._index[key])
        return tuple.__getitem__(self, key)

    def keys(self) -> List[str]:
        return list(self._names)


def _row_factory(cursor):
    desc = cursor.description
    if desc is None:
        return tuple
    names = [d.name for d in desc]
    index = {n: i for i, n in enumerate(names)}
    return lambda values: Row(values, names, index)


# ------------------------------------------------------------ placeholders

_TOKENS = re.compile(r"""'(?:[^']|'')*'|"(?:[^"]|"")*"|--[^\n]*|::|\?|:[A-Za-z_]\w*|%""")


@lru_cache(maxsize=512)
def to_pg(sql: str, with_params: bool = True) -> str:
    """`?` -> %s, `:name` -> %(name)s, literal % -> %% (only when params are passed)."""
    if not with_params:
        return sql

    def sub(m: "re.Match[str]") -> str:
        t = m.group(0)
        if t == "?":
            return "%s"
        if t == "%":
            return "%%"
        if t == "::":
            return t
        if t[0] == ":":
            return f"%({t[1:]})s"
        return t.replace("%", "%%")  # quoted text / comment

    return _TOKENS.sub(sub, sql)


Params = Union[Sequence[Any], Dict[str, Any], None]


def _adapt(params: Params):
    if params is None:
        return None
    if isinstance(params, dict):
        return params
    return tuple(params)


class Conn:
    """Thin wrapper over a psycopg connection with sqlite3-style helpers."""

    __slots__ = ("raw",)

    def __init__(self, raw: psycopg.Connection):
        self.raw = raw

    def execute(self, sql: str, params: Params = None) -> psycopg.Cursor:
        has = bool(params) or isinstance(params, dict)
        query, args = to_pg(sql, has), (_adapt(params) if has else None)
        # Outside a transaction a statement can safely be repeated, so a
        # connection that died since its last use (database restart, Neon
        # waking up, network blip) is replaced and the statement retried once.
        retryable = self.raw.info.transaction_status == pq.TransactionStatus.IDLE
        try:
            cur = self.raw.cursor()
            cur.execute(query, args)
            return cur
        except psycopg.OperationalError:
            if not (retryable and (self.raw.closed or self.raw.broken)):
                raise
        self.raw = raw_connection()
        cur = self.raw.cursor()
        cur.execute(query, args)
        return cur

    def executemany(self, sql: str, seq: Iterable[Params]) -> psycopg.Cursor:
        cur = self.raw.cursor()
        rows = [_adapt(p) for p in seq]
        if rows:
            cur.executemany(to_pg(sql, True), rows)
        return cur


# ------------------------------------------------------------- connections

_local = threading.local()
_PING_AFTER = 30.0  # seconds idle before checking the connection still works


def _open(url: str) -> psycopg.Connection:
    return psycopg.connect(
        url,
        autocommit=True,
        prepare_threshold=None,  # PgBouncer (Neon pooler) can't keep prepared statements
        row_factory=_row_factory,
        connect_timeout=10,
        application_name="topolearn-api",
        # Notice a silently vanished server instead of hanging on it.
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=3,
        tcp_user_timeout=15000,
    )


class _Owner:
    """Closes its connection as soon as the thread-local holding it goes away.

    The web server's thread pool retires idle threads; without this, their
    connections would stay open until Python's garbage collector happened to
    run (psycopg objects can sit in reference cycles), piling up connections."""

    __slots__ = ("conn",)

    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    def __del__(self):
        try:
            self.conn.close()
        except Exception:
            pass


def _close_local() -> None:
    owner = getattr(_local, "owner", None)
    _local.conn = None
    _local.owner = None
    if owner is not None:
        try:
            owner.conn.close()
        except Exception:
            pass


def raw_connection() -> psycopg.Connection:
    url = config.DATABASE_URL
    if not url:
        raise DatabaseMissing(
            "DATABASE_URL is not set. Put your Postgres/Neon connection string in the "
            "environment or in a .env file next to the app."
        )
    conn: Optional[psycopg.Connection] = getattr(_local, "conn", None)
    if conn is not None and (conn.closed or conn.broken or getattr(_local, "url", None) != url):
        _close_local()
        conn = None
    idle = conn is not None and conn.info.transaction_status == pq.TransactionStatus.IDLE
    if conn is not None and idle and time.monotonic() - getattr(_local, "used", 0.0) > _PING_AFTER:
        try:
            conn.execute("SELECT 1")
        except psycopg.Error:
            _close_local()
            conn = None
    if conn is None:
        try:
            conn = _open(url)
        except psycopg.OperationalError as exc:
            raise DatabaseMissing(f"Can't reach the database: {exc}".strip()) from exc
        _local.conn, _local.url, _local.owner = conn, url, _Owner(conn)
    _local.used = time.monotonic()
    return conn


def get_conn() -> Conn:
    """This thread's connection, for reads (autocommit)."""
    return Conn(raw_connection())


@contextmanager
def transaction() -> Iterator[Conn]:
    """Commit on success, roll back on error. Nested calls become savepoints.

    If the connection turns out to be dead when the transaction starts, it is
    replaced and the BEGIN retried (nothing has happened yet, so that's safe).
    A connection lost *during* a transaction raises; the work is never replayed."""
    raw = raw_connection()
    outermost = raw.info.transaction_status == pq.TransactionStatus.IDLE
    with ExitStack() as stack:
        try:
            stack.enter_context(raw.transaction())
        except psycopg.OperationalError:
            if not (outermost and (raw.closed or raw.broken)):
                raise
            raw = raw_connection()
            stack.enter_context(raw.transaction())
        yield Conn(raw)


def reset_connections() -> None:
    """Drop this thread's connection (tests, or after the URL changes)."""
    _close_local()


def graph_loaded(conn: Conn) -> bool:
    row = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema = ? AND table_name = 'concepts'", (GRAPH,)
    ).fetchone()
    return row is not None
