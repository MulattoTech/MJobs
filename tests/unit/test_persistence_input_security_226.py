"""SQL-looking profile text remains data at public persistence boundaries."""
from copy import deepcopy

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import NoResultFound

from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


@pytest.mark.parametrize('name', [
    "Robert'); DROP TABLE career_profiles;--",
    "' OR 1=1 --",
    "Café — 李 & O'Neil",
])
def test_sql_looking_profile_values_round_trip_without_changing_schema_or_query_scope(tmp_path, name):
    url = f"sqlite:///{(tmp_path / 'security.db').as_posix()}"
    store = Store(url)
    try:
        store.migrate()
        store.bind_workspace_mode('FIXTURE')
        profile = deepcopy(PROFILE)
        profile['name'] = name
        store.save_profile(profile)
        assert store.profile(profile['id'])['name'] == name
        assert store.count('career_profiles') == 1
        assert inspect(store.engine).has_table('career_profiles')
        with pytest.raises(NoResultFound):
            store.profile("fixture' OR '1'='1")
    finally:
        store.engine.dispose()

    reopened = Store(url)
    try:
        assert reopened.profile('fixture')['name'] == name
        assert reopened.count('career_profiles') == 1
    finally:
        reopened.engine.dispose()
