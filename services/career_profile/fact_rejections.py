"""Explicit rejection of a pending immutable fact version; eligibility is unchanged."""
import hashlib
import json
import re

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from packages.persistence import models as m
from packages.persistence.store import identity, now


ACTION = 'CAREER_FACT_REJECTED'
HEX = re.compile(r'[0-9a-f]{64}\Z')
CONTENT_FIELDS = ('fact_id', 'kind', 'text', 'source', 'confidence', 'verified', 'user_approved')


class FactRejectionError(ValueError):
    def __init__(self, code, status_code=409):
        super().__init__(code)
        self.code, self.status_code = code, status_code


def _require(condition, code, status=409):
    if not condition:
        raise FactRejectionError(code, status)


def fact_sha256(fact):
    return hashlib.sha256(json.dumps({key: fact[key] for key in CONTENT_FIELDS},
        ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _version(connection, profile_id, *, lock=False):
    if lock and connection.dialect.name == 'sqlite':
        connection.exec_driver_sql('BEGIN IMMEDIATE')
    statement = select(m.profiles.c.version).where(m.profiles.c.id == profile_id)
    if lock and connection.dialect.name == 'postgresql':
        statement = statement.with_for_update()
    version = connection.execute(statement).scalar_one_or_none()
    _require(version is not None, 'FACT_REJECTION_PROFILE_NOT_FOUND', 404)
    return version


def _decision_id(profile_id, version, fact_id, checksum):
    return identity('career-fact-rejection', profile_id, version, fact_id, checksum)


def list_fact_rejections(store, profile_id):
    with store.engine.connect() as connection:
        version = _version(connection, profile_id)
        facts = connection.execute(select(m.facts).where(m.facts.c.profile_id == profile_id,
            m.facts.c.version == version).order_by(m.facts.c.fact_id)).mappings().all()
        identities = {_decision_id(profile_id, version, fact['fact_id'], fact_sha256(fact)) for fact in facts}
        events = {row['id']: row for row in connection.execute(select(m.audit).where(
            m.audit.c.entity_id == profile_id, m.audit.c.action == ACTION, m.audit.c.id.in_(identities)
        )).mappings()} if identities else {}
        rows = []
        for fact in facts:
            checksum = fact_sha256(fact)
            decision_id = _decision_id(profile_id, version, fact['fact_id'], checksum)
            event = events.get(decision_id)
            expected = dict(profile_id=profile_id, profile_version=version, fact_id=fact['fact_id'],
                fact_sha256=checksum, decision='REJECT')
            rejected = (not fact['verified'] and not fact['user_approved'] and event is not None
                and event['payload'] == expected)
            rows.append(dict(fact_id=fact['fact_id'], fact_sha256=checksum,
                review_status=('REJECTED' if rejected else 'APPROVED' if
                    fact['verified'] and fact['user_approved'] else 'PENDING'),
                decision_id=decision_id if rejected else None,
                reviewed_at=event['timestamp'] if rejected else None))
        # A concurrent profile save cannot make this projection current for the
        # new version. Its explicit version must match the client's saved profile.
        return dict(profile_id=profile_id, profile_version=version, facts=rows)


def reject_fact(store, profile_id, fact_id, *, expected_profile_version, expected_fact_sha256, confirmed):
    _require(confirmed is True and isinstance(expected_profile_version, str)
        and HEX.fullmatch(expected_profile_version) and isinstance(expected_fact_sha256, str)
        and HEX.fullmatch(expected_fact_sha256), 'FACT_REJECTION_REQUEST_INVALID', 422)
    with store.engine.begin() as connection:
        version = _version(connection, profile_id, lock=True)
        _require(version == expected_profile_version, 'FACT_REJECTION_VERSION_CHANGED')
        fact = connection.execute(select(m.facts).where(m.facts.c.profile_id == profile_id,
            m.facts.c.version == version, m.facts.c.fact_id == fact_id)).mappings().one_or_none()
        _require(fact is not None, 'FACT_REJECTION_FACT_NOT_FOUND', 404)
        checksum = fact_sha256(fact)
        _require(checksum == expected_fact_sha256, 'FACT_REJECTION_CONTENT_CHANGED')
        _require(not fact['verified'] and not fact['user_approved'], 'FACT_REJECTION_REQUIRES_PENDING_FACT')
        decision_id = _decision_id(profile_id, version, fact_id, checksum)
        payload = dict(profile_id=profile_id, profile_version=version, fact_id=fact_id,
            fact_sha256=checksum, decision='REJECT')
        insert = {'sqlite': sqlite_insert, 'postgresql': postgres_insert}.get(connection.dialect.name)
        _require(insert is not None, 'FACT_REJECTION_BACKEND_UNSUPPORTED', 422)
        connection.execute(insert(m.audit).values(id=decision_id, entity_id=profile_id,
            action=ACTION, payload=payload, timestamp=now()).on_conflict_do_nothing(index_elements=['id']))
        stored = connection.execute(select(m.audit).where(m.audit.c.id == decision_id)).mappings().one()
        _require(stored['entity_id'] == profile_id and stored['action'] == ACTION
            and stored['payload'] == payload, 'FACT_REJECTION_RECEIPT_CONFLICT')
        return dict(payload, decision_id=decision_id, reviewed_at=stored['timestamp'])
