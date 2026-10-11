"""Profile-scoped, read-only API for the source relationship graph."""
from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, HTTPException
from sqlalchemy.exc import NoResultFound

from services.operations.source_relationship_graph import read_source_relationship_graph


_NODE_METADATA_FIELDS = {
    "company": ("company_id", "domain"),
    "company_job_source": (
        "company_source_id", "provider", "tenant", "origin",
        "collection_strategy", "enabled",
    ),
    "canonical_job": (
        "job_id", "company_id", "requisition", "location", "remote_type",
    ),
    "job_posting": ("posting_id", "source", "source_job_id", "fetched_at"),
    "application_route": ("route_id", "provider", "tenant", "requisition"),
    "application": (
        "application_id", "state", "target", "created_at", "submitted_at",
    ),
}


def _invalid_graph():
    raise HTTPException(
        status_code=500,
        detail={"code": "SOURCE_RELATIONSHIP_GRAPH_RESPONSE_INVALID"},
    )


def _project_graph(graph) -> tuple[list[dict], list[dict]]:
    if not isinstance(graph, Mapping):
        _invalid_graph()
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        _invalid_graph()

    safe_nodes = []
    for node in nodes:
        if not isinstance(node, Mapping):
            _invalid_graph()
        node_id = node.get("id")
        node_type = node.get("type")
        label = node.get("label")
        metadata = node.get("metadata")
        metadata_fields = _NODE_METADATA_FIELDS.get(node_type)
        if (not isinstance(node_id, str) or not node_id
                or not isinstance(node_type, str) or metadata_fields is None
                or not isinstance(label, str) or not isinstance(metadata, Mapping)):
            _invalid_graph()
        safe_metadata = {key: metadata[key] for key in metadata_fields if key in metadata}
        if any(value is not None and type(value) not in (str, bool, int)
               for value in safe_metadata.values()):
            _invalid_graph()
        safe_nodes.append({
            "id": node_id,
            "type": node_type,
            "label": label,
            "metadata": safe_metadata,
        })

    safe_edges = []
    for edge in edges:
        if not isinstance(edge, Mapping):
            _invalid_graph()
        edge_id = edge.get("id")
        source = edge.get("source")
        target = edge.get("target")
        edge_type = edge.get("type")
        if any(not isinstance(value, str) or not value
               for value in (edge_id, source, target, edge_type)):
            _invalid_graph()
        safe_edges.append({
            "id": edge_id,
            "source": source,
            "target": target,
            "type": edge_type,
        })
    return safe_nodes, safe_edges


def create_source_relationship_graph_router(store):
    """Build a read-only router for one profile's persisted relationship graph."""
    router = APIRouter()

    @router.get("/profiles/{profile_id}/source-relationship-graph")
    def get_source_relationship_graph(profile_id: str):
        try:
            graph = read_source_relationship_graph(store, profile_id)
        except NoResultFound:
            raise HTTPException(
                status_code=404,
                detail={"code": "SOURCE_RELATIONSHIP_GRAPH_PROFILE_NOT_FOUND"},
            ) from None
        nodes, edges = _project_graph(graph)
        return {"profile_id": profile_id, "nodes": nodes, "edges": edges}

    return router


__all__ = ["create_source_relationship_graph_router"]
