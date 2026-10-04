"""Bind public-site discovery only to corroborated domains, never claimed names."""
from urllib.parse import urlsplit
from contextlib import nullcontext
from sqlalchemy import select
from packages.persistence import models as m
from packages.persistence.store import identity
from services.company_discovery.domains import (normalize_domain, is_aggregator_domain,
    lock_domain_evidence, VERIFIED_METHODS)
from services.company_discovery.public_client import PublicCompanyClient, PublicFetchError


def resolve_public_company(store, candidate, *, connection=None, lock_verified=False):
    name=str(candidate.get('company_name') or '').strip()
    website=str(candidate.get('company_website') or '').strip()
    if not name:
        raise PublicFetchError('COMPANY_NAME_REQUIRED')
    client=PublicCompanyClient(website)
    client.close()
    parsed=urlsplit(website)
    host=parsed.hostname.lower().rstrip('.').encode('idna').decode('ascii')
    origin='https://'+host
    domain=normalize_domain(origin)
    if not domain or is_aggregator_domain(domain):
        raise PublicFetchError('COMPANY_DOMAIN_REVIEW_REQUIRED')
    with (nullcontext(connection) if connection is not None else store.engine.connect()) as connection:
        if lock_verified:
            lock_domain_evidence(connection, domain)
        query=select(m.company_domains).where(
            m.company_domains.c.domain==domain,m.company_domains.c.verified.is_(True))
        matches=list(connection.execute(query).mappings())
        matches=[row for row in matches if row['verification_method'] in VERIFIED_METHODS and row['evidence']]
        owners={row['company_id'] for row in matches}
        if len(owners)>1:
            raise PublicFetchError('COMPANY_IDENTITY_REVIEW_REQUIRED')
        if owners:
            owner=next(iter(owners))
            company=connection.execute(select(m.companies).where(m.companies.c.id==owner)).mappings().one()
            return dict(company_id=owner,name=company['name'],domain=domain,is_new=False,
                        confidence=max(row['confidence'] for row in matches),evidence=['VERIFIED_DOMAIN_MATCH'])
        company_id=identity('public-company-origin',origin)
        existing=connection.execute(select(m.companies).where(m.companies.c.id==company_id)).mappings().one_or_none()
    return dict(company_id=company_id,name=existing['name'] if existing else name,domain=domain,
                is_new=not bool(existing),confidence=0.45,evidence=['CANDIDATE_DOMAIN_UNVERIFIED'])
