"""Opt-in, process-owned PostgreSQL templates for disposable CI fixtures only."""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
from unittest.mock import patch
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

SCHEMA = 'aggregator_bootstrap_fixture'
PREFIX = 'mjobs_test_ci_'


def disposable_url(value, *, env=None):
    env = os.environ if env is None else env
    # libpq's PGHOSTADDR can supersede a validated URL host. Disallow inherited
    # endpoint/service/options configuration before creating any fixture engine.
    overrides = {"PGHOST", "PGHOSTADDR", "PGPORT", "PGDATABASE", "PGSERVICE",
                 "PGSERVICEFILE", "PGOPTIONS"}
    present = sorted({key.upper() for key, val in env.items()
                      if key.upper() in overrides and val})
    if present:
        raise ValueError("Inherited PostgreSQL endpoint/options overrides are forbidden: "
                         + ", ".join(present))
    url = make_url(value)
    if (url.drivername not in {'postgresql', 'postgresql+psycopg', 'postgresql+psycopg2'}
            or url.host not in {'localhost', '127.0.0.1', '::1'}
            or not url.database or not (url.database == 'mjobs_test' or url.database.startswith('mjobs_test_'))
            or url.query):
        raise ValueError('An explicit loopback disposable mjobs_test database URL is required')
    # Pin numeric loopback hostaddr and port in every derived URL, including
    # migration, Store, clone and cleanup connections. Do not rely on DNS or
    # on environment defaults changing between validation and connection.
    host = '127.0.0.1' if url.host == 'localhost' else url.host
    port = 5432 if url.port is None else url.port
    if not 1 <= port <= 65535:
        raise ValueError('An explicit loopback disposable mjobs_test database URL is required')
    return url.set(host=host, port=port).update_query_dict({'hostaddr': host})


def require_template_inputs(env):
    """Opt-in is an assertion, never a reason to silently skip the proof."""
    if not env.get('MJOBS_TEST_POSTGRES_URL'):
        raise ValueError('Requested PostgreSQL template requires MJOBS_TEST_POSTGRES_URL')
    if not re.fullmatch(r'[0-9a-f]{64}', env.get('MJOBS_CI_POSTGRES_DUMP_CONTAINER', '')):
        raise ValueError('Requested PostgreSQL template requires an exact dump container ID')
    return disposable_url(env['MJOBS_TEST_POSTGRES_URL'], env=env)


class OwnedDatabaseTemplate:
    """Clone a freshly migrated, connection-disabled database; never reuse user data."""

    def __init__(self, url):
        self.base = disposable_url(url)
        self.admin = create_engine(self.base, isolation_level='AUTOCOMMIT',
                                   connect_args={'connect_timeout': 5}, hide_parameters=True)
        self.owned: list[str] = []
        self.template: str | None = None

    def _create(self, source='template0'):
        if source != 'template0' and source != self.template:
            raise ValueError('Only a pristine or owned migrated template is allowed')
        name = PREFIX + uuid4().hex
        with self.admin.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{name}" TEMPLATE "{source}"')
        self.owned.append(name)
        return name

    def schema_url(self, name):
        if name not in self.owned:
            raise ValueError('Database is not owned by this fixture')
        return self.base.set(database=name).update_query_dict({
            'options': f'-csearch_path={SCHEMA} -clock_timeout=5000 -cstatement_timeout=30000',
        }).render_as_string(hide_password=False)

    def fresh(self):
        """Independently migrate a clean database for template creation or equivalence checks."""
        name = self._create()
        url = self.schema_url(name)
        engine = create_engine(url, connect_args={'connect_timeout': 5}, hide_parameters=True)
        try:
            with engine.begin() as connection:
                connection.execute(text(f'CREATE SCHEMA "{SCHEMA}"'))
            with patch.dict(os.environ, {'MJOBS_DATABASE_URL': url}):
                command.upgrade(Config('alembic.ini'), 'head')
        finally:
            engine.dispose()
        return name, url

    def start(self):
        if self.template is not None:
            raise ValueError('Template already initialized')
        name, _ = self.fresh()
        with self.admin.connect() as connection:
            connection.exec_driver_sql(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS false')
        self.template = name

    def clone(self):
        if self.template is None:
            raise ValueError('Template not initialized')
        name = self._create(self.template)
        return name, self.schema_url(name)

    def drop(self, name):
        # Only drop databases successfully created by this exact instance, never the base.
        suffix = name.removeprefix(PREFIX)
        if (name not in self.owned or not name.startswith(PREFIX) or len(suffix) != 32
                or any(char not in '0123456789abcdef' for char in suffix)):
            raise ValueError('Refusing to drop a database not owned by this fixture')
        with self.admin.connect() as connection:
            # No FORCE: a leaked test connection is a qualification failure.
            connection.exec_driver_sql(f'DROP DATABASE "{name}"')
        self.owned.remove(name)

    def close(self):
        failures = []
        try:
            for name in reversed(self.owned.copy()):
                try:
                    self.drop(name)
                except (SQLAlchemyError, ValueError) as error:
                    failures.append(error)
        finally:
            self.admin.dispose()
        if failures:
            raise ExceptionGroup('Owned fixture database cleanup failed', failures)


def canonical_dump(raw: bytes) -> bytes:
    # PostgreSQL 17+ emits randomized psql restrict tokens. Keep every
    # actual SQL statement, schema object, owner, ACL and data row.
    return b'\n'.join(line for line in raw.splitlines()
                       if not line.startswith((b'\\restrict ', b'\\unrestrict ')))


def verify_template(pool: OwnedDatabaseTemplate, container_id: str) -> dict:
    """Independently migrate, dump, mutate and compare two isolated clones."""
    if not re.fullmatch(r'[0-9a-f]{64}', container_id):
        raise ValueError('Expected the exact disposable PostgreSQL container ID')

    def dump(name):
        if name not in pool.owned:
            raise ValueError('Cannot dump a database not owned by this fixture')
        raw = subprocess.check_output([
            'docker', 'exec', container_id, 'pg_dump', '--no-password',
            '--username', pool.base.username, '--dbname', name,
        ], timeout=60)
        return canonical_dump(raw)

    def measured(operation):
        started = time.perf_counter()
        value = operation()
        return value, time.perf_counter() - started

    (fresh, _), migration_seconds = measured(pool.fresh)
    (first, first_url), first_seconds = measured(pool.clone)
    (second, _), second_seconds = measured(pool.clone)
    baseline = dump(fresh)
    if not baseline or baseline != dump(first) or baseline != dump(second):
        raise ValueError('Native clone differs from independently migrated database')
    engine = create_engine(first_url, connect_args={'connect_timeout': 5}, hide_parameters=True)
    try:
        with engine.begin() as connection:
            if connection.execute(text('SELECT current_database()')).scalar_one() != first:
                raise ValueError('Clone is connected to a different database')
            if connection.execute(text('SELECT current_schema()')).scalar_one() != SCHEMA:
                raise ValueError('Clone search path differs from the fixture')
            for setting, expected in (('lock_timeout', 5000), ('statement_timeout', 30000)):
                value = connection.execute(text('SELECT setting::int FROM pg_settings WHERE name=:name'),
                                           {'name': setting}).scalar_one()
                if value != expected:
                    raise ValueError('Clone fixture timeout differs')
            connection.execute(text('CREATE TABLE ci_template_isolation_probe (id integer PRIMARY KEY)'))
            connection.execute(text('INSERT INTO ci_template_isolation_probe VALUES (1)'))
    finally:
        engine.dispose()
    if dump(first) == baseline or dump(second) != baseline or dump(fresh) != baseline:
        raise ValueError('Database clone isolation or negative control failed')
    third, _ = pool.clone()
    if dump(third) != baseline:
        raise ValueError('A newly cloned database inherited another fixture mutation')
    for name in (third, second, first, fresh):
        pool.drop(name)
    return {
        'full_dump_sha256': hashlib.sha256(baseline).hexdigest(),
        'fresh_migration_seconds': round(migration_seconds, 3),
        'clone_seconds': [round(first_seconds, 3), round(second_seconds, 3)],
        'schema_data_owner_acl_equivalence': True,
        'cross_clone_and_future_clone_isolation': True,
    }
