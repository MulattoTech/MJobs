"""Transaction locks for source portals that share a parent host."""
from __future__ import annotations

import hashlib

from sqlalchemy import text

from services.company_discovery.domains import normalize_domain


def _related_public_hosts(values):
    hosts = set()
    for value in values:
        host = normalize_domain(value)
        if not host:
            continue
        labels = host.split(".")
        # Lock every hostname suffix that can overlap another portal host, but
        # omit the final label because locking a public suffix would serialize
        # unrelated employers (for example, all .com sites).
        hosts.update(".".join(labels[index:]) for index in range(len(labels) - 1))
    return sorted(hosts)


def lock_company_source_hosts(connection, values):
    """Serialize related-host source writes and ownership scans for this txn.

    Each portal hostname locks itself and all parent suffixes with two or more
    labels. Acquiring the sorted set gives multi-URL portals a stable lock order.
    """
    hosts = _related_public_hosts(values)
    if connection.dialect.name == "postgresql":
        for host in hosts:
            digest = hashlib.sha256(
                b"mjobs-company-source-host-v1\0" + host.encode("utf-8")
            ).digest()
            key = int.from_bytes(digest[:8], "big", signed=True)
            connection.execute(
                text("SELECT pg_advisory_xact_lock(:source_host_key)"),
                {"source_host_key": key},
            )
    return hosts
