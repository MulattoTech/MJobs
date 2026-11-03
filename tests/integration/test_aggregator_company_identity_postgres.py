"""Conflicting aggregator identities serialize on disposable PostgreSQL."""
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text

from packages.persistence import models as m
from packages.persistence import resume_workspace  # noqa: F401
from packages.persistence.store import Store, identity
from services.dedupe.ingestion_identity import lock_company_ingestion
from services.discovery.sources import collect as collector
from services.discovery.sources.collect import configure_source
from services.discovery.sources.errors import SourceError
from services.discovery.sources.wwr import company_slug
from tests.integration.test_aggregator_company_identity import CONFLICT, collect_names
from tests.integration.test_lever_form_review_flow import _disposable_postgres_url


@pytest.fixture
def postgres_store():
    supplied = os.environ.get("MJOBS_AI_EVALUATION_POSTGRES_URL")
    if not supplied:
        pytest.skip("Requires disposable loopback MJOBS_AI_EVALUATION_POSTGRES_URL")
    url = _disposable_postgres_url(supplied)
    schema = "mjobs_company_identity_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text('CREATE SCHEMA "' + schema + '"'))
    database_url = url.update_query_dict({
        "options": "-csearch_path=" + schema + " -clock_timeout=5000 -cstatement_timeout=20000",
    }).render_as_string(hide_password=False)
    store = Store(database_url)
    try:
        m.metadata.create_all(store.engine)
        yield store, database_url
    finally:
        store.engine.dispose()
        with admin.begin() as connection:
            connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
        admin.dispose()


@pytest.mark.parametrize("names", (
    ("Synthetic A+B", "Synthetic A B"),
    ("公司甲", "公司乙"),
), ids=("punctuation", "non_ascii"))
def test_concurrent_collectors_preserve_one_company_and_refuse_the_conflicting_contender(
    postgres_store, names,
):
    store, database_url = postgres_store
    source_ids = [
        configure_source(store, "race-" + str(index), provider="wwr", rate_limit=600)
        for index in range(2)
    ]
    company_id = identity("wwr", company_slug(names[0]))
    assert company_id == identity("wwr", company_slug(names[1]))
    holder = store.engine.connect()
    transaction = holder.begin()
    lock_company_ingestion(holder, [company_id])
    both_entered = threading.Event()
    counter_lock = threading.Lock()
    entered = []

    def observed_lock(connection, company_ids):
        with counter_lock:
            entered.append(threading.get_ident())
            if len(entered) == 2:
                both_entered.set()
        lock_company_ingestion(connection, company_ids)

    def run(index):
        contender = Store(database_url)
        try:
            collect_names(
                contender, "wwr", [names[index]], source_id=source_ids[index],
                first_id=8400 + index,
            )
            return "PASS", index, None
        except SourceError as error:
            return "REFUSED", index, error.code
        finally:
            contender.engine.dispose()

    with (
        patch.object(collector, "lock_company_ingestion", observed_lock),
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        futures = [executor.submit(run, index) for index in range(2)]
        try:
            assert both_entered.wait(5), "Both collectors must reach the real company lock"
        finally:
            transaction.rollback()
            holder.close()
        results = [future.result(timeout=15) for future in futures]
    assert sorted(result[0] for result in results) == ["PASS", "REFUSED"]
    winner = next(result[1] for result in results if result[0] == "PASS")
    refusal = next(result for result in results if result[0] == "REFUSED")
    assert refusal[2] == CONFLICT
    reopened = Store(database_url)
    try:
        with reopened.engine.connect() as connection:
            company = connection.execute(select(m.companies)).mappings().one()
            job = connection.execute(select(m.jobs)).mappings().one()
            posting = connection.execute(select(m.postings)).mappings().one()
            sources = {
                row["id"]: row for row in connection.execute(select(m.job_sources)).mappings()
            }
        assert company["id"] == job["company_id"] == company_id
        assert company["name"] == names[winner]
        assert posting["job_id"] == job["id"]
        assert str(8400 + winner) in job["requisition"]
        assert sources[source_ids[winner]]["health_status"] == "HEALTHY"
        assert sources[source_ids[refusal[1]]]["last_error"] == CONFLICT
    finally:
        reopened.engine.dispose()
