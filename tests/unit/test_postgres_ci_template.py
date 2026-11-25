"""Reject unsafe targets and preserve all substantive PostgreSQL dump contents."""
import pytest

from fixtures.postgres_ci_template import (
    OwnedDatabaseTemplate,
    canonical_dump,
    disposable_url,
)


@pytest.mark.parametrize('url', [
    'sqlite:///mjobs_test',
    'postgresql+psycopg://mjobs@database.example/mjobs_test',
    'postgresql+psycopg://mjobs@127.0.0.1/mjobs',
    'postgresql+psycopg://mjobs@127.0.0.1/mjobs_test?options=anything',
])
def test_template_requires_disposable_loopback_database(url):
    with pytest.raises(ValueError, match='loopback disposable'):
        disposable_url(url)


def test_template_cannot_drop_or_use_unowned_database():
    pool = OwnedDatabaseTemplate('postgresql+psycopg://mjobs@127.0.0.1:1/mjobs_test')
    try:
        for name in ('mjobs_test', 'mjobs_test_ci_' + 'a' * 32):
            with pytest.raises(ValueError, match='not owned'):
                pool.drop(name)
            with pytest.raises(ValueError, match='not owned'):
                pool.schema_url(name)
        with pytest.raises(ValueError, match='not initialized'):
            pool.clone()
    finally:
        pool.close()


def test_dump_equivalence_ignores_only_random_psql_tokens():
    a = b'\\restrict tokenA\nCREATE TABLE x (id integer);\n\\unrestrict tokenA\n'
    b = b'\\restrict tokenB\nCREATE TABLE x (id integer);\n\\unrestrict tokenB\n'
    assert canonical_dump(a) == canonical_dump(b)
    assert canonical_dump(a) != canonical_dump(b.replace(b'integer', b'text'))
    assert canonical_dump(a + b'GRANT SELECT ON x TO mjobs;\n') != canonical_dump(a)
