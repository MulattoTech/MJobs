"""Read-only, deterministic application capability classification for routes."""

from services.applications.knowledge import application_adapter_knowledge


_CAPABILITIES = frozenset({
    "FIXTURE_ONLY",
    "UNSUPPORTED",
    "REVIEW_REQUIRED",
    "UNKNOWN",
})


def effective_application_capability(connection, route_id):
    """Return the effective capability for one persisted application route.

    The exact route and the workspace's persisted mode are resolved by
    ``application_adapter_knowledge``. This projection does not inspect job or
    provider text, perform network access or ATS detection, or authorize any
    adapter execution or submission. Its result is always one of the four
    capability values listed above; unexpected knowledge statuses fail closed
    to ``UNKNOWN``.
    """
    knowledge = application_adapter_knowledge(connection, route_id)
    capability = knowledge.get("adapter_status")
    return capability if isinstance(capability, str) and capability in _CAPABILITIES else "UNKNOWN"
