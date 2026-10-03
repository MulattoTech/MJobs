"""Bind sampled link observations to immutable candidates in the import transaction."""
from datetime import datetime, timezone
import re
from uuid import uuid4

from sqlalchemy import select

from packages.persistence import models as m
from services.discovery.feed_items import describe_feed_item


def _time(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        raise ValueError('LINK_EVIDENCE_TIME_INVALID') from None


def validate_reports(items, strategy, endpoint, reports, at):
    """Reject stale/cross-feed reports before they can affect quality or selection."""
    if reports is None:
        return {}
    reports = list(reports)
    if len(reports) > 5:
        raise ValueError('LINK_EVIDENCE_LIMIT')
    accepted = {}
    for item in items:
        entry = describe_feed_item(item, strategy, endpoint)
        if entry:
            if entry.external_key in accepted and accepted[entry.external_key] != entry:
                raise ValueError('SOURCE_DUPLICATE_JOB_ID')
            accepted[entry.external_key] = entry
    bound = {}
    reference = _time(at)
    for report in reports:
        entry = accepted.get(report.external_key)
        if (entry is None or report.external_key in bound or report.endpoint != endpoint
                or entry.raw_sha256 != report.raw_sha256 or entry.application_url != report.requested_url):
            raise ValueError('LINK_EVIDENCE_IDENTITY_MISMATCH')
        observed = _time(report.observed_at)
        if observed > reference or (reference - observed).total_seconds() > 300:
            raise ValueError('LINK_EVIDENCE_TIME_INVALID')
        if (report.result not in {'ACCURATE', 'INACCURATE', 'UNKNOWN'}
                or not re.fullmatch(r'[A-Z0-9_]{1,100}', report.reason)
                or (report.response_sha256 is not None and not re.fullmatch(r'[0-9a-f]{64}', report.response_sha256))
                or (report.response_status is not None and (type(report.response_status) is not int
                                                          or not 100 <= report.response_status <= 599))
                or not isinstance(report.identity_evidence, (tuple, list))
                or len(report.identity_evidence) > 12
                or any(not isinstance(value, str) or len(value) > 200 for value in report.identity_evidence)):
            raise ValueError('LINK_EVIDENCE_INVALID')
        if (report.result == 'ACCURATE' and (report.response_status != 200 or not report.response_sha256 or not report.final_url)
                or report.result == 'INACCURATE' and report.response_status not in {404, 410}):
            raise ValueError('LINK_EVIDENCE_CONCLUSION_INVALID')
        bound[report.external_key] = report
    return bound


def record_link_evidence(connection, candidate_id, report):
    candidate = connection.execute(select(m.posting_value_candidates).where(
        m.posting_value_candidates.c.id == candidate_id)).mappings().one()
    snapshot = connection.execute(select(m.posting_snapshots).where(
        m.posting_snapshots.c.id == candidate['posting_snapshot_id'])).mappings().one()
    if (snapshot['sha256'] != report.raw_sha256 or candidate['application_url'] != report.requested_url
            or candidate['normalized']['attributes']['source_endpoint'] != report.endpoint):
        raise ValueError('LINK_EVIDENCE_CANDIDATE_MISMATCH')
    connection.execute(m.source_application_link_observations.insert().values(
        id=uuid4().hex, candidate_id=candidate_id, observed_at=_time(report.observed_at).isoformat(),
        requested_url=report.requested_url, final_url=report.final_url, result=report.result,
        reason=report.reason, response_status=report.response_status, response_sha256=report.response_sha256,
        identity_evidence=list(report.identity_evidence)))
