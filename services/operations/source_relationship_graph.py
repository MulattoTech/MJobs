"""Read-only, profile-scoped view of company, posting and application links."""

import unicodedata

from sqlalchemy import select

from packages.persistence import models as m


COMPANY_EVENTS = ("COMPANY_DISCOVERED", "COMPANY_SOURCE_ENROLLED")


def _safe_text(value, limit=256):
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", value)
    printable = "".join(character if character.isprintable() else " "
                        for character in normalized)
    return " ".join(printable.split())[:limit] or None


def _node(node_type, identifier, label, metadata):
    return {
        "id": f"{node_type}:{identifier}",
        "type": node_type,
        "label": label,
        "metadata": metadata,
    }


def _edge(edge_type, source, target):
    return {
        "id": f"{edge_type}:{source}>{target}",
        "source": source,
        "target": target,
        "type": edge_type,
    }


def read_source_relationship_graph(store, profile_id):
    """Return only graph records visible through this profile's durable links.

    Job visibility comes from profile matches or applications. Company-only
    visibility comes from this profile's discovery/enrollment audit events.
    Posting-source links are included only when a persisted value candidate
    links the source and a posting for a visible job.
    """
    store.profile(profile_id)
    with store.engine.connect() as connection:
        events = list(connection.execute(select(
            m.audit.c.action, m.audit.c.payload,
        ).where(
            m.audit.c.entity_id == profile_id,
            m.audit.c.action.in_(COMPANY_EVENTS),
        )).mappings())
        matched_job_ids = set(connection.execute(select(
            m.matches.c.job_id,
        ).where(m.matches.c.profile_id == profile_id)).scalars())
        applications = [dict(row) for row in connection.execute(select(
            m.applications,
        ).where(m.applications.c.profile_id == profile_id)).mappings()]

        visible_job_ids = matched_job_ids | {row["job_id"] for row in applications}
        jobs = ([dict(row) for row in connection.execute(select(m.jobs).where(
            m.jobs.c.id.in_(visible_job_ids),
        )).mappings()] if visible_job_ids else [])
        visible_job_ids = {row["id"] for row in jobs}

        enrolled_company_ids = set()
        enrolled_source_ids = set()
        for event in events:
            payload = event["payload"] if isinstance(event["payload"], dict) else {}
            company_id = payload.get("company_id")
            source_id = payload.get("company_source_id")
            if isinstance(company_id, str) and company_id:
                enrolled_company_ids.add(company_id)
            if isinstance(source_id, str) and source_id:
                enrolled_source_ids.add(source_id)

        company_ids = enrolled_company_ids | {row["company_id"] for row in jobs}
        companies = ([dict(row) for row in connection.execute(select(m.companies).where(
            m.companies.c.id.in_(company_ids),
        )).mappings()] if company_ids else [])
        company_by_id = {row["id"]: row for row in companies}
        company_ids = set(company_by_id)

        postings = ([dict(row) for row in connection.execute(select(m.postings).where(
            m.postings.c.job_id.in_(visible_job_ids),
        )).mappings()] if visible_job_ids else [])
        posting_ids = {row["id"] for row in postings}
        candidate_source_ids = set()
        if posting_ids:
            candidate_source_ids = set(connection.execute(select(
                m.posting_value_candidates.c.company_source_id,
            ).where(m.posting_value_candidates.c.posting_id.in_(posting_ids))).scalars())

        source_ids = enrolled_source_ids | candidate_source_ids
        sources = ([dict(row) for row in connection.execute(select(m.company_sources).where(
            m.company_sources.c.id.in_(source_ids),
            m.company_sources.c.company_id.in_(company_ids),
        )).mappings()] if source_ids and company_ids else [])
        source_by_id = {row["id"]: row for row in sources}
        source_ids = set(source_by_id)

        posting_source_links = set()
        if posting_ids and source_ids:
            posting_source_links = {
                (row["company_source_id"], row["posting_id"])
                for row in connection.execute(select(
                    m.posting_value_candidates.c.company_source_id,
                    m.posting_value_candidates.c.posting_id,
                ).where(
                    m.posting_value_candidates.c.posting_id.in_(posting_ids),
                    m.posting_value_candidates.c.company_source_id.in_(source_ids),
                )).mappings()
            }

        routes = ([dict(row) for row in connection.execute(select(m.routes).where(
            m.routes.c.job_id.in_(visible_job_ids),
        )).mappings()] if visible_job_ids else [])

    nodes = []
    edges = []
    node_ids = set()

    def add_node(node):
        if node["id"] not in node_ids:
            node_ids.add(node["id"])
            nodes.append(node)

    for company in companies:
        add_node(_node("company", company["id"], _safe_text(company["name"]) or "Company", {
            "company_id": company["id"],
            "domain": (_safe_text(company["domain"], 253) or "").casefold().rstrip(".") or None,
        }))

    for source in sources:
        company_node = f"company:{source['company_id']}"
        source_node = f"company_job_source:{source['id']}"
        if company_node not in node_ids:
            continue
        provider = _safe_text(source["provider"]) or "unknown"
        tenant = _safe_text(source["tenant"]) or "unknown"
        add_node(_node("company_job_source", source["id"],
                       f"{provider} · {tenant}", {
                           "company_source_id": source["id"],
                           "provider": provider,
                           "tenant": tenant,
                           "origin": _safe_text(source["origin"]) or "UNKNOWN",
                           "collection_strategy": _safe_text(source["collection_strategy"]) or "UNKNOWN",
                           "enabled": bool(source["enabled"]),
                       }))
        edges.append(_edge("company_source", company_node, source_node))

    for job in jobs:
        add_node(_node("canonical_job", job["id"], _safe_text(job["title"]) or "Job", {
            "job_id": job["id"],
            "company_id": job["company_id"],
            "requisition": _safe_text(job["requisition"]),
            "location": _safe_text(job["location"]),
            "remote_type": _safe_text(job["remote_type"]) or "UNKNOWN",
        }))

    for posting in postings:
        job_node = f"canonical_job:{posting['job_id']}"
        posting_node = f"job_posting:{posting['id']}"
        if job_node not in node_ids:
            continue
        add_node(_node("job_posting", posting["id"],
                       _safe_text(posting["source_job_id"]) or "Posting", {
            "posting_id": posting["id"],
            "source": _safe_text(posting["source"]) or "unknown",
            "source_job_id": _safe_text(posting["source_job_id"]),
            "fetched_at": _safe_text(posting["fetched_at"], 64),
        }))
        edges.append(_edge("job_posting", job_node, posting_node))

    for source_id, posting_id in posting_source_links:
        source_node = f"company_job_source:{source_id}"
        posting_node = f"job_posting:{posting_id}"
        if source_node in node_ids and posting_node in node_ids:
            edges.append(_edge("source_posting", source_node, posting_node))

    route_by_id = {row["id"]: row for row in routes}
    for route in routes:
        job_node = f"canonical_job:{route['job_id']}"
        route_node = f"application_route:{route['id']}"
        if job_node not in node_ids:
            continue
        provider = _safe_text(route["provider"]) or "unknown"
        requisition = _safe_text(route["requisition"]) or "unknown"
        add_node(_node("application_route", route["id"],
                       f"{provider} · {requisition}", {
                           "route_id": route["id"],
                           "provider": provider,
                           "tenant": _safe_text(route["tenant"]),
                           "requisition": requisition,
                       }))
        edges.append(_edge("job_route", job_node, route_node))

    for application in applications:
        route = route_by_id.get(application["route_id"])
        if route is None or route["job_id"] != application["job_id"]:
            continue
        route_node = f"application_route:{route['id']}"
        application_node = f"application:{application['id']}"
        if route_node not in node_ids:
            continue
        state = _safe_text(application["state"]) or "UNKNOWN"
        add_node(_node("application", application["id"], state, {
            "application_id": application["id"],
            "state": state,
            "target": _safe_text(application["target"]) or "UNKNOWN",
            "created_at": _safe_text(application["created_at"], 64),
            "submitted_at": _safe_text(application["submitted_at"], 64),
        }))
        edges.append(_edge("route_application", route_node, application_node))

    nodes.sort(key=lambda node: (node["type"], node["label"].casefold(), node["id"]))
    unique_edges = {edge["id"]: edge for edge in edges}
    edges = sorted(unique_edges.values(), key=lambda edge: (
        edge["type"], edge["source"], edge["target"],
    ))
    return {"nodes": nodes, "edges": edges}
