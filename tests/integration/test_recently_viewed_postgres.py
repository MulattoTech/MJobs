"""Real disposable PostgreSQL migration and serialized job-visit history."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import threading
from unittest.mock import patch
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, func, inspect, select, text, update

from packages.persistence import models as m
from packages.persistence.store import Store
from services.tracking.recently_viewed import (
    clear_recent_job_views, list_recent_job_views, record_job_view,
)
from tests.integration.test_lever_form_review_flow import _disposable_postgres_url
from tests.integration.test_spine import PROFILE

PARENT_REVISION = "20261025di01"
VISITS_REVISION = "20261104rv01"


@pytest.fixture
def recent_postgres():
    supplied = os.environ.get("MJOBS_AI_EVALUATION_POSTGRES_URL")
    if not supplied:
        pytest.skip("Requires disposable loopback MJOBS_AI_EVALUATION_POSTGRES_URL")
    url = _disposable_postgres_url(supplied)
    schema = "mjobs_recent_views_" + uuid4().hex
    admin = create_engine(url)
    store = None
    created = False
    try:
        with admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + schema + '"'))
        created = True
        database_url = url.update_query_dict({
            "options": "-csearch_path=" + schema + " -clock_timeout=5000 -cstatement_timeout=20000",
        }).render_as_string(hide_password=False)
        root = Path(__file__).resolve().parents[2]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "migrations"))
        config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
        with patch.dict(os.environ, {"MJOBS_DATABASE_URL": database_url}):
            command.upgrade(config, PARENT_REVISION)
            store = Store(database_url)
            # Seed an existing source-schema workspace. The new Store binding
            # guard correctly requires every target table, including visits.
            with store.engine.begin() as connection:
                connection.execute(m.workspace_identity.insert().values(
                    id="workspace", operation_mode="FIXTURE", bound_at="2026-11-05T00:00:00Z"))
            store.save_profile(dict(PROFILE))
            store.save_profile({**PROFILE, "id": "other", "name": "Other",
                                "email": "other@example.invalid"})
            with store.engine.begin() as connection:
                connection.execute(m.companies.insert().values(
                    id="recent-company", name="Synthetic Employer", domain="employer.example"))
                connection.execute(m.jobs.insert(), [{
                    "id": job, "company_id": "recent-company", "requisition": job,
                    "title": "Synthetic role " + job, "description": "Fixture only",
                    "skills": [], "location": "Remote", "remote_type": "REMOTE_US",
                    "attributes": {"status": "ACTIVE", "fictional": True},
                } for job in ("recent-a", "recent-b")])
            yield store, database_url, config
    finally:
        if store is not None:
            store.engine.dispose()
        if created:
            with admin.begin() as connection:
                connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
        admin.dispose()


def _legacy_snapshot(store):
    with store.engine.connect() as connection:
        return {
            "profiles": connection.execute(select(m.profiles).order_by(m.profiles.c.id)).mappings().all(),
            "jobs": connection.execute(select(m.jobs).order_by(m.jobs.c.id)).mappings().all(),
            "workspace": connection.execute(text("SELECT * FROM workspace_identity")).mappings().all(),
        }


def test_postgres_upgrade_preserves_legacy_state_and_history_blocks_downgrade(recent_postgres):
    store, database_url, config = recent_postgres
    before = _legacy_snapshot(store)
    command.upgrade(config, VISITS_REVISION)
    assert _legacy_snapshot(store) == before
    assert "profile_job_views" in inspect(store.engine).get_table_names()
    assert list_recent_job_views(store, "fixture")["items"] == []
    record_job_view(store, "fixture", "recent-a")
    record_job_view(store, "other", "recent-b")
    first = list_recent_job_views(store, "fixture")
    second = list_recent_job_views(store, "other")
    store.engine.dispose()
    reopened = Store(database_url)
    try:
        assert list_recent_job_views(reopened, "fixture") == first
        assert list_recent_job_views(reopened, "other") == second
        with pytest.raises(RuntimeError, match="Refusing to discard recently viewed job history"):
            command.downgrade(config, PARENT_REVISION)
        with reopened.engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == VISITS_REVISION
        assert list_recent_job_views(reopened, "fixture") == first
        assert list_recent_job_views(reopened, "other") == second
        assert clear_recent_job_views(reopened, "fixture")["cleared"] == 1
        assert list_recent_job_views(reopened, "other") == second
        assert clear_recent_job_views(reopened, "other")["cleared"] == 1
        command.downgrade(config, PARENT_REVISION)
        assert "profile_job_views" not in inspect(reopened.engine).get_table_names()
        assert _legacy_snapshot(reopened) == before
    finally:
        reopened.engine.dispose()


def test_postgres_concurrent_reopens_deduplicate_and_lifecycle_read_is_read_only(recent_postgres):
    store, database_url, config = recent_postgres
    command.upgrade(config, VISITS_REVISION)
    record_job_view(store, "other", "recent-a")
    other_before = list_recent_job_views(store, "other")
    entered = threading.Barrier(2)

    def visit(job_id):
        contender = Store(database_url)
        try:
            entered.wait(timeout=5)
            for _ in range(4):
                record_job_view(contender, "fixture", job_id)
        finally:
            contender.engine.dispose()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(visit, job) for job in ("recent-a", "recent-b")]
        for future in futures:
            future.result(timeout=20)
    with store.engine.connect() as connection:
        visits_before = connection.execute(select(m.profile_job_views).where(
            m.profile_job_views.c.profile_id == "fixture").order_by(
                m.profile_job_views.c.visit_order.desc())).mappings().all()
        application_count = connection.execute(select(func.count()).select_from(m.applications)).scalar_one()
    assert len(visits_before) == 2
    assert visits_before[0]["visit_order"] == 8
    assert len({row["visit_order"] for row in visits_before}) == 2
    assert [row["job_id"] for row in list_recent_job_views(store, "fixture")["items"]] == [
        row["job_id"] for row in visits_before]
    assert list_recent_job_views(store, "other") == other_before
    with store.engine.begin() as connection:
        connection.execute(update(m.jobs).where(m.jobs.c.id == "recent-b").values(
            attributes={"status": "EXPIRED", "fictional": True}))
    closed = next(row for row in list_recent_job_views(store, "fixture")["items"]
                  if row["job_id"] == "recent-b")
    assert closed["available"] is False
    assert closed["unavailable_reason"] == "NO_LONGER_OPEN"
    assert closed["title"] == "Synthetic role recent-b"
    with store.engine.connect() as connection:
        assert connection.execute(select(m.profile_job_views).where(
            m.profile_job_views.c.profile_id == "fixture").order_by(
                m.profile_job_views.c.visit_order.desc())).mappings().all() == visits_before
        assert connection.execute(select(func.count()).select_from(m.applications)).scalar_one() == application_count
