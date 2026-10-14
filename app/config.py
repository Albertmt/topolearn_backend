import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


DB_PATH = Path(os.environ.get("TOPOLEARN_DB", ROOT / "data" / "topolearn.db"))
# Writable user accounts (kept apart from the read-only concept DB).
USERS_DB_PATH = Path(os.environ.get("TOPOLEARN_USERS_DB", ROOT / "data" / "users.db"))

# Comma-separated list of allowed browser origins.
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()]

# Hard caps so one request can never walk the whole graph.
MAX_DEPTH = _int("MAX_DEPTH", 30)
DEFAULT_DEPTH = _int("DEFAULT_DEPTH", 20)
MAX_NODES = _int("MAX_NODES", 500)
DEFAULT_NODES = _int("DEFAULT_NODES", 200)
SEARCH_LIMIT = _int("SEARCH_LIMIT", 20)
