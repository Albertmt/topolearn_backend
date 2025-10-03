from fastapi import FastAPI, HTTPException, Query
from neo4j import GraphDatabase
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# Allow CORS (for Flutter dev server)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # can restrict to specific origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Neo4j connection
URI = "bolt://localhost:7687"
AUTH = ("neo4j", "Armenak529")
driver = GraphDatabase.driver(URI, auth=AUTH)

@app.get("/")
def read_root():
    return {"message": "Hello Topolearn!"}


@app.get("/search")
def search_concepts(q: str = Query(..., description="Partial concept name")):
    """
    Autocomplete endpoint. Returns concepts whose name contains the query.
    """
    with driver.session(database="topolearndb") as session:
        cypher = """
        MATCH (c:Concept)
        WHERE toLower(c.name) CONTAINS toLower($q)
        RETURN c LIMIT 10
        """
        result = session.run(cypher, q=q)
        concepts = [dict(record["c"]) for record in result]
        return {"concepts": concepts}


@app.get("/concept")
def get_concept(name: str = Query(..., description="Concept id or name")):
    """
    Fetch a concept by name or id, including its prerequisites recursively.
    """
    with driver.session(database="topolearndb") as session:
        cypher = """
        MATCH path = (c:Concept)-[:REQUIRES*0..]->(prereq)
        WHERE c.id = $name OR toLower(c.name) = toLower($name)
        WITH COLLECT(DISTINCT nodes(path)) AS paths
        UNWIND paths AS n_list
        UNWIND n_list AS n
        RETURN DISTINCT n
        """
        result = session.run(cypher, name=name)
        concepts = [dict(record["n"]) for record in result]
        if not concepts:
            raise HTTPException(status_code=404, detail="Concept not found")
        return {"concepts": concepts}
