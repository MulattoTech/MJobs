"""DISCOVER dispatch binds collector lineage to the claimed parent attempt."""
import inspect
import os
from unittest.mock import patch

from sqlalchemy import select, update

from packages.persistence import models as m
from workers import tasks as worker_tasks


def test_discover_dispatch_passes_operation_and_attempt_to_collect_source():
    # Reuse the real API/SQLite/worker fixture; its adapter returns bounded
    # synthetic items and never opens a provider connection.
    from tests.integration.test_agent_browser_budget_entrypoint_sustain_403 import (
        AgentBrowserBudgetEntrypointSustain403Tests,
    )

    fixture = AgentBrowserBudgetEntrypointSustain403Tests("runTest")
    fixture.setUp()
    try:
        operation_id = fixture._operation()
        with fixture.store.engine.begin() as connection:
            queued = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == operation_id)).mappings().one())
            params = dict(queued["params"])
            # Scheduler-created DISCOVER operations omit a profile snapshot.
            params.pop("profile_version", None)
            connection.execute(update(m.operations).where(
                m.operations.c.id == operation_id).values(params=params))

        expected_attempt = int(queued["attempt_count"]) + 1
        observed = []
        original_collect_source = worker_tasks.collect_source
        supported_parameters = set(inspect.signature(original_collect_source).parameters)

        def observe_collect_source(*args, **kwargs):
            observed.append((args, dict(kwargs)))
            # The test can run against the current pre-dispatch-patch checkout:
            # capture the proposed call boundary, then strip only arguments the
            # current collector has not gained yet. Once collector lineage lands,
            # the real collector receives the complete kwargs unchanged.
            unsupported = {"parent_operation_id", "parent_operation_attempt"} - supported_parameters
            actual_kwargs = {key: value for key, value in kwargs.items() if key not in unsupported}
            return original_collect_source(*args, **actual_kwargs)

        with patch.object(worker_tasks, "collect_source", new=observe_collect_source):
            result = fixture._execute(operation_id)

        assert result["status"] == "COMPLETED", result
        assert len(observed) == 1
        args, kwargs = observed[0]
        assert args[0].url == fixture.store.url
        assert args[1:3] == (fixture.source_id, "fixture")
        assert kwargs.get("parent_operation_id") == operation_id
        assert kwargs.get("parent_operation_attempt") == expected_attempt
        assert kwargs["profile_version"] is None

        with fixture.store.engine.connect() as connection:
            completed = connection.execute(select(m.operations).where(
                m.operations.c.id == operation_id)).mappings().one()
        assert completed["attempt_count"] == expected_attempt
        assert completed["state"] == "COMPLETED"
    finally:
        fixture.tearDown()
