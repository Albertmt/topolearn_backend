import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Read KEY=value lines from a local .env file (git-ignored) into the
    environment, without overriding variables that are already set."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if value[:1] in ('"', "'") and value[0] in value[1:]:
            value = value[1 : value.index(value[0], 1)]  # quoted: take what's inside, drop any comment after
        else:
            value = value.split(" #", 1)[0].strip()  # unquoted: a " #" starts a comment
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(ROOT / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# Postgres connection string. In production this is the Neon *pooled* URL
# (host contains "-pooler"); locally any Postgres, e.g. a Neon `dev` branch.
# Everything lives there: concepts and prerequisites (schema "graph", loaded by
# scripts/build_db.py) and user data (schema "public", created on first use).
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

# Comma-separated list of allowed browser origins.
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()]

# Hard caps so one request can never walk the whole graph.
MAX_DEPTH = _int("MAX_DEPTH", 30)
DEFAULT_DEPTH = _int("DEFAULT_DEPTH", 20)
MAX_NODES = _int("MAX_NODES", 500)
DEFAULT_NODES = _int("DEFAULT_NODES", 200)
SEARCH_LIMIT = _int("SEARCH_LIMIT", 20)

# Sign-in: the Firebase project whose ID tokens we accept (its project id, e.g.
# "topolearn-gcloud"). Without it, every signed-in endpoint answers 503.
FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "").strip()

# The website, for links in emails (verification, password reset).
SITE_URL = os.environ.get("SITE_URL", "https://topolearnapp.com").rstrip("/")

# Transactional email through Resend. Without the key, emails are logged, not sent.
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
EMAIL_FROM = os.environ.get("EMAIL_FROM", "Topolearn <do-not-reply@topolearnapp.com>")
EMAIL_REPLY_TO = os.environ.get("EMAIL_REPLY_TO", "support@topolearnapp.com")

# Who may review problem reports: comma-separated emails, signed in with a verified address.
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
