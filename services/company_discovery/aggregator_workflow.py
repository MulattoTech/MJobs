"""Execute staged aggregator-to-native discovery in the company worker only."""
from services.company_discovery.aggregator_bootstrap import (
    BootstrapBudget, corroborate_aggregator_job,
)
from services.company_discovery.domain_acquisition import acquire_official_domain_from_aggregator_bootstrap
from services.company_discovery.domains import normalize_domain
from services.company_discovery.public_client import PublicFetchError
from services.company_discovery.public_resolution import resolve_public_company
from services.company_discovery.requests import (
    AGGREGATOR_BOOTSTRAP_PENDING, AGGREGATOR_BOOTSTRAP_VERIFIED,
    authorize_discovery_request, load_aggregator_bootstrap_context,
    persist_aggregator_bootstrap_proof,
)


def execute_aggregator_bootstrap(store, operation, *, check, total_timeout,
                                 resolver_client_factory=None):
    """Reauthorize, corroborate outside transactions, persist, and enroll safely."""
    if (operation.get('kind') != 'COMPANY_DISCOVER'
            or operation.get('params', {}).get('fetch_mode') != 'PUBLIC_HTTPS'
            or not operation.get('params', {}).get('discovery_request_id')):
        raise PublicFetchError('DISCOVERY_AUTHORIZATION_REQUIRED')
    if total_timeout < 2:
        raise PublicFetchError('BOOTSTRAP_DEADLINE')

    def authorized_check():
        check()
        authorization = authorize_discovery_request(store, operation)
        if authorization.get('authorization') not in {
                AGGREGATOR_BOOTSTRAP_PENDING, AGGREGATOR_BOOTSTRAP_VERIFIED}:
            raise PublicFetchError('DISCOVERY_AUTHORIZATION_CHANGED')
        return authorization

    def enrollment_guard(connection):
        check()
        return authorize_discovery_request(store, operation, connection=connection)

    authorization = authorized_check()
    if authorization['authorization'] == AGGREGATOR_BOOTSTRAP_PENDING:
        context = load_aggregator_bootstrap_context(store, operation)
        remaining = getattr(check, 'remaining_seconds', None)
        timeout_seconds = min(30, float(remaining()) if callable(remaining) else float(total_timeout))
        if timeout_seconds < 2:
            raise PublicFetchError('BOOTSTRAP_DEADLINE')
        kwargs = {}
        if resolver_client_factory is not None:
            kwargs['client_factory'] = resolver_client_factory
        receipt = corroborate_aggregator_job(
            context['raw'], context['normalized'], context['source_snapshot'],
            context['profile_snapshot'], source_policy=context['source_policy'],
            check=authorized_check,
            budget=BootstrapBudget(timeout_seconds=timeout_seconds), **kwargs)
        if receipt is None:
            raise PublicFetchError('COMPANY_DOMAIN_CORROBORATION_REQUIRED')
        authorized_check()
        persist_aggregator_bootstrap_proof(store, operation, receipt)
        authorization = authorized_check()
    if authorization['authorization'] != AGGREGATOR_BOOTSTRAP_VERIFIED:
        raise PublicFetchError('DISCOVERY_AUTHORIZATION_CHANGED')
    receipt = authorization.get('aggregator_bootstrap_receipt')
    if not receipt:
        raise PublicFetchError('DISCOVERY_BOOTSTRAP_PROOF_CHANGED')

    if authorization.get('aggregator_bootstrap_domain') is None:
        acquire_official_domain_from_aggregator_bootstrap(
            store, authorization, authorized_check, enrollment_guard)
        authorization = authorized_check()
    domain = authorization.get('aggregator_bootstrap_domain')
    if not domain or domain.get('website') is None:
        raise PublicFetchError('DISCOVERY_DOMAIN_AUTHORIZATION_CHANGED')

    candidate = {
        'company_name': receipt['normalized_projection']['company_name'],
        'company_website': receipt['employer_url'],
    }
    resolved = resolve_public_company(store, candidate)
    if (resolved.get('company_id') != authorization.get('company_id')
            or 'VERIFIED_DOMAIN_MATCH' not in resolved.get('evidence', [])
            or resolved.get('domain') != normalize_domain(domain['website'])):
        raise PublicFetchError('DISCOVERY_COMPANY_IDENTITY_CHANGED')

    from services.company_discovery.ats_enrollment import enroll_public_ats
    return enroll_public_ats(
        store, candidate, resolved, receipt['employer_url'], '',
        profile_id=authorization['aggregator_bootstrap_pending']['binding']['profile_id'],
        profile_version=authorization['aggregator_bootstrap_pending']['binding']['profile_version'],
        check=authorized_check, enrollment_guard=enrollment_guard,
        provider='greenhouse', aggregator_bootstrap_authorization=authorization,
    )
