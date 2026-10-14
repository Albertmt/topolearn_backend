from typing import List, Optional

from pydantic import BaseModel, Field


class Concept(BaseModel):
    id: str = Field(..., description="Stable public id (slug)")
    name: str
    type: Optional[str] = None
    domain: Optional[str] = None
    description: Optional[str] = None
    example: Optional[str] = Field(None, description="Worked example or analogy")
    wiki_url: Optional[str] = None
    difficulty: Optional[float] = None
    prereq_count: int = Field(0, description="Number of direct prerequisites")


class GraphNode(Concept):
    depth: int = Field(..., description="Shortest number of steps from the root")
    hidden_prereqs: int = Field(
        0, description="Direct prerequisites not included in this response (expand to see them)"
    )
    understanding: Optional[int] = Field(None, description="User's self-rated understanding 0-100")
    status: Optional[str] = Field(None, description="User flag: need_to_learn | hidden")


class GraphEdge(BaseModel):
    concept: str = Field(..., description="Concept id that has the prerequisite")
    requires: str = Field(..., description="Concept id that must be learned first")
    why: Optional[str] = Field(None, description="Why the concept needs this prerequisite")


class PrereqGraph(BaseModel):
    root: str
    nodes: List[GraphNode]
    edges: List[GraphEdge]
    truncated: bool = Field(
        False, description="True if depth or node limits cut the graph short"
    )


class SearchResponse(BaseModel):
    query: str
    results: List[Concept]


class UnlocksResponse(BaseModel):
    id: str
    results: List[Concept]


class Health(BaseModel):
    status: str
    concepts: int
    search: str
