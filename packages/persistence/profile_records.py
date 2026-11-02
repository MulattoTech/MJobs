"""ORM mappings over the existing immutable career-profile schema.

These read models reuse the migration-owned Tables; they create no parallel
schema. Sessions remain short-lived and never own a caller's transaction.
"""
from copy import deepcopy

from sqlalchemy.orm import registry

from packages.persistence import models

mapper_registry = registry(metadata=models.metadata)


@mapper_registry.mapped
class CareerProfileRecord:
    __table__ = models.profiles


@mapper_registry.mapped
class ProfileVersionRecord:
    __table__ = models.profile_versions


@mapper_registry.mapped
class CareerFactRecord:
    __table__ = models.facts


def snapshot(record):
    """Detach plain values, including nested JSON, from the Session identity map."""
    return {column.key: deepcopy(getattr(record, column.key))
            for column in record.__table__.columns}
