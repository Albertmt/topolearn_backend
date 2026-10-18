"""Topolearn API.

Run locally:
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8001
"""
import psycopg
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from . import auth, catalog, config, learning, repository, social, users
from .db import DatabaseMissing, get_conn, graph_loaded

# Every request that names a learner (`user_id` in the path or query) must come
# from that learner, signed in: see auth.py.
app = FastAPI(title="Topolearn API", version="1.1.0", dependencies=[Depends(auth.require_self)])

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.include_router(users.router)
app.include_router(catalog.router)
app.include_router(catalog.pages)
app.include_router(learning.router)
app.include_router(social.router)

_NOT_LOADED = "The tool catalog isn't loaded yet. Load it with: python scripts/build_db.py"


@app.exception_handler(DatabaseMissing)
def _db_missing(_, exc: DatabaseMissing):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(psycopg.errors.UndefinedTable)
def _graph_missing(_, exc: psycopg.errors.UndefinedTable):
    detail = _NOT_LOADED if "graph." in str(exc) else "Database tables are missing; see the logs."
    return JSONResponse(status_code=503, content={"detail": detail})


@app.exception_handler(psycopg.DataError)
def _bad_text(_, exc: psycopg.DataError):
    # e.g. a NUL character in a query string or body, which Postgres text can't hold.
    return JSONResponse(status_code=422, content={"detail": "The request contains characters that aren't allowed."})


@app.exception_handler(psycopg.OperationalError)
def _db_unreachable(_, exc: psycopg.OperationalError):
    # Lost connection, Neon waking up too slowly, ... — safe for the app to retry.
    return JSONResponse(status_code=503, content={"detail": "The database is temporarily unavailable. Try again."})


@app.get("/", include_in_schema=False)
def root():
    return {"name": "topolearn", "docs": "/docs", "health": "/health"}


@app.get("/health")
def health():
    conn = get_conn()
    if not graph_loaded(conn):
        raise DatabaseMissing(_NOT_LOADED)
    return {"status": "ok", "tools": repository.count_tools(conn), "lessons": repository.count_lessons(conn)}
