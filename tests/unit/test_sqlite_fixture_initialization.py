"""Atomic fixture DDL must preserve the exact SQLite schema and durability."""
from pathlib import Path

import pytest
from sqlalchemy import event

from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


def schema(store):
    with store.engine.connect() as connection:
        objects = connection.exec_driver_sql(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        ).all()
        settings = [connection.exec_driver_sql(f"PRAGMA {name}").scalar_one()
                    for name in ("journal_mode", "synchronous", "foreign_keys")]
    return objects, settings


@pytest.mark.parametrize("memory", [False, True])
def test_fixture_ddl_matches_legacy_schema_and_preserves_existing_profile(tmp_path: Path, memory):
    from packages.persistence import resume_workspace  # noqa: F401

    urls = ["sqlite:///:memory:"] * 2 if memory else [
        f"sqlite:///{(tmp_path / name).as_posix()}" for name in ("legacy.db", "atomic.db")
    ]
    old, new = [Store(url) for url in urls]
    try:
        m.metadata.create_all(old.engine)
        new.migrate()
        assert schema(new) == schema(old)
        assert schema(new)[1][2] == 1
        new.bind_workspace_mode("FIXTURE")
        new.save_profile(PROFILE)
        saved = new.profile("fixture")
        before = schema(new)
        new.migrate()
        assert new.profile("fixture") == saved
        assert schema(new) == before
        with new.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        old.engine.dispose()
        new.engine.dispose()
    if not memory:
        reopened = Store(urls[1])
        try:
            assert reopened.profile("fixture") == saved
            assert schema(reopened) == before
        finally:
            reopened.engine.dispose()


def test_interrupted_fixture_ddl_rolls_back_all_new_objects_and_can_retry(tmp_path: Path):
    store = Store(f"sqlite:///{(tmp_path / 'rollback.db').as_posix()}")
    created = []
    before = schema(store)

    def interrupt(connection, cursor, statement, *args):
        if statement.lstrip().upper().startswith("CREATE TABLE"):
            created.append(statement)
            if len(created) == 3:
                raise RuntimeError("synthetic fixture DDL interruption")

    event.listen(store.engine, "before_cursor_execute", interrupt)
    try:
        with pytest.raises(RuntimeError, match="synthetic fixture DDL interruption"):
            store.migrate()
        assert len(created) == 3
        assert schema(store) == before
    finally:
        event.remove(store.engine, "before_cursor_execute", interrupt)
    try:
        store.migrate()
        assert len(schema(store)[0]) > 100
        store.bind_workspace_mode("FIXTURE")
        store.save_profile(PROFILE)
        assert store.profile("fixture")["id"] == "fixture"
    finally:
        store.engine.dispose()
