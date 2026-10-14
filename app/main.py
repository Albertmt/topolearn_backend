"""Topolearn API.

Run locally:
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8001
"""
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from . import config, learning, repository, users
from .db import DatabaseMissing, get_conn, has_fts
from .schemas import Concept, Health, PrereqGraph, SearchResponse, UnlocksResponse

app = FastAPI(title="Topolearn API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.include_router(users.router)
app.include_router(learning.router)
app.include_router(learning.pages)

# The data only changes when the DB is rebuilt, so let browsers/CDNs cache.
_CACHE = "public, max-age=300, stale-while-revalidate=3600"


@app.exception_handler(DatabaseMissing)
def _db_missing(_, exc: DatabaseMissing):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.get("/", include_in_schema=False)
def root():
    return {"name": "topolearn", "docs": "/docs", "health": "/health"}


@app.get("/health", response_model=Health)
def health():
    conn = get_conn()
    return {
        "status": "ok",
        "concepts": repository.count_concepts(conn),
        "search": "fts5" if has_fts(conn) else "like",
    }


@app.get("/api/v1/search", response_model=SearchResponse)
def search(
    response: Response,
    q: str = Query(..., min_length=1, max_length=100, description="What the user typed"),
    limit: int = Query(10, ge=1, le=config.SEARCH_LIMIT),
):
    response.headers["Cache-Control"] = _CACHE
    return {"query": q, "results": repository.search(get_conn(), q, limit)}


@app.get("/api/v1/concepts/{concept_id}", response_model=Concept)
def concept(concept_id: str, response: Response):
    found = repository.get_concept(get_conn(), concept_id)
    if found is None:
        raise HTTPException(404, f"Unknown concept '{concept_id}'")
    response.headers["Cache-Control"] = _CACHE
    return found


@app.get("/api/v1/concepts/{concept_id}/graph", response_model=PrereqGraph)
def prerequisite_graph(
    concept_id: str,
    response: Response,
    depth: int = Query(config.DEFAULT_DEPTH, ge=1, le=config.MAX_DEPTH),
    max_nodes: int = Query(config.DEFAULT_NODES, ge=1, le=config.MAX_NODES),
    user_id: Optional[str] = Query(
        None, description="Personalise: drop this user's hidden concepts and include their progress"
    ),
):
    skip = learning.hidden_concepts(user_id) if user_id else set()
    graph = repository.prerequisite_graph(get_conn(), concept_id, depth, max_nodes, skip=skip)
    if graph is None:
        raise HTTPException(404, f"Unknown concept '{concept_id}'")
    if user_id:
        progress = learning.progress_for(user_id, [n["id"] for n in graph["nodes"]])
        for n in graph["nodes"]:
            p = progress.get(n["id"])
            if p:
                n.update(p)
        response.headers["Cache-Control"] = "private, no-store"  # personal data
    else:
        response.headers["Cache-Control"] = _CACHE
    return graph


@app.get("/api/v1/concepts/{concept_id}/unlocks", response_model=UnlocksResponse)
def unlocks(
    concept_id: str,
    response: Response,
    limit: int = Query(20, ge=1, le=100),
):
    found = repository.unlocks(get_conn(), concept_id, limit)
    if found is None:
        raise HTTPException(404, f"Unknown concept '{concept_id}'")
    response.headers["Cache-Control"] = _CACHE
    return {"id": concept_id, "results": found}
