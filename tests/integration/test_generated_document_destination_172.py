"""Synthetic generation destinations stay inside the configured artifact root."""
import hashlib
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest
from sqlalchemy import select

from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store, identity
from services.discovery.pipeline import discover
from services.documents import generate
from services.documents.errors import PreparationError
from tests.integration.test_spine import PROFILE


@pytest.fixture
def generation(tmp_path):
    store = Store(f"sqlite:///{(tmp_path / 'state.db').as_posix()}")
    store.migrate()
    profile_id = '../../outside'
    store.save_profile({**PROFILE, 'id': profile_id})
    with fixture_server() as origin:
        discover(store, origin, profile_id)
    job_id = store.jobs(profile_id)[0]['id']
    output = tmp_path / 'approved'
    output.mkdir()
    try:
        yield store, profile_id, job_id, output
    finally:
        store.engine.dispose()


def directory_alias(link, target):
    if os.name == 'nt':
        # Both paths are exclusively owned by pytest's synthetic temp fixture.
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)],
                       check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.mark.parametrize('alias_level', ['application', 'profile'])
def test_existing_alias_rejected_before_artifact_write(generation, tmp_path, alias_level):
    store, profile_id, job_id, output = generation
    outside = tmp_path / 'outside'
    outside.mkdir()
    sentinel = outside / 'sentinel.txt'
    sentinel.write_bytes(b'unrelated synthetic document')
    application_root = output / identity(job_id, profile_id)
    link = application_root
    if alias_level == 'profile':
        application_root.mkdir()
        link = application_root / store.profile(profile_id)['version']
    before = {name: store.count(name) for name in ('applications', 'documents', m.audit.name)}
    directory_alias(link, outside)
    try:
        with patch.object(generate, '_generate_document_artifact',
                          wraps=generate._generate_document_artifact) as writer:
            with pytest.raises(PreparationError) as rejected:
                generate.prepare(store, job_id, profile_id, output)
            assert rejected.value.code == 'DOCUMENT_DESTINATION_UNSAFE'
            writer.assert_not_called()
        assert sentinel.read_bytes() == b'unrelated synthetic document'
        assert list(outside.iterdir()) == [sentinel]
        assert {name: store.count(name) for name in before} == before
        reopened = Store(store.url)
        try:
            assert {name: reopened.count(name) for name in before} == before
        finally:
            reopened.engine.dispose()
    finally:
        if os.name == 'nt':
            link.rmdir()
        else:
            link.unlink()


def test_traversal_shaped_profile_id_generates_inside_root_and_reopens(generation):
    store, profile_id, job_id, output = generation
    application_id = generate.prepare(store, job_id, profile_id, output)
    assert application_id == identity(job_id, profile_id)
    reopened = Store(store.url)
    try:
        assert reopened.application(application_id)['state'] == 'PREPARED'
        with reopened.engine.connect() as connection:
            documents = connection.execute(select(m.documents).where(
                m.documents.c.application_id == application_id)).mappings().all()
        assert {document['kind'] for document in documents} == {'resume', 'cover-letter'}
        for document in documents:
            for extension, relative in document['files'].items():
                path = (output / application_id / relative).resolve()
                assert path.is_relative_to(output.resolve())
                assert hashlib.sha256(path.read_bytes()).hexdigest() == document['sha256'][extension]
    finally:
        reopened.engine.dispose()
