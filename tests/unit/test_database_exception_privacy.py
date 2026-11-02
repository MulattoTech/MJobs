"""The persistence engine never formats bound private values into errors."""
from copy import deepcopy

import pytest
from sqlalchemy.exc import IntegrityError

from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


def test_real_constraint_error_hides_parameters_and_rolls_back(tmp_path):
    store = Store(f"sqlite:///{(tmp_path / 'privacy.db').as_posix()}")
    canary = "synthetic-private-contact-parameter"
    try:
        store.migrate()
        profile = deepcopy(PROFILE)
        store.save_profile(profile)
        with pytest.raises(IntegrityError) as failure:
            with store.engine.begin() as connection:
                connection.execute(m.profiles.insert().values(
                    id=profile["id"], name=canary,
                    email=canary + "@example.invalid", version="synthetic"))
        assert canary not in str(failure.value)
        assert canary not in repr(failure.value)
        assert store.profile(profile["id"])["name"] == profile["name"]
        assert store.count("career_profiles") == 1
    finally:
        store.engine.dispose()
