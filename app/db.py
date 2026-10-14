"""Read-only SQLite access.

The database is built offline (scripts/build_db.py) and shipped next to the
API, so every server instance has its own local copy: no network hop, no
connection pool, and horizontal scaling is just "run more copies".
"""
import os
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from . import config

_local = threading.local()


class DatabaseMissing(RuntimeError):
    pass


def _open(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise DatabaseMissing(
            f"Database not found at {path}. Build it with: python scripts/build_db.py"
        )
    conn = sqlite3.connect(
        f"file:{path}?mode=ro", uri=True, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA cache_size = -32000")  # ~32 MB page cache per thread
    conn.execute("PRAGMA mmap_size = 268435456")  # 256 MB memory-mapped reads
    return conn


def get_conn() -> sqlite3.Connection:
    """One connection per worker thread, reopened if the DB file is rebuilt."""
    path = config.DB_PATH
    try:
        mtime: Optional[float] = os.stat(path).st_mtime
    except FileNotFoundError:
        mtime = None
    conn = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "mtime", None) != mtime:
        if conn is not None:
            conn.close()
        _local.conn = _open(path)
        _local.mtime = mtime
    return _local.conn


def has_fts(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'concepts_fts'"
    ).fetchone()
    return row is not None
