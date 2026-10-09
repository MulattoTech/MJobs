import React from 'react';

export type ApplicationAdapterKnowledge = {
  provider?: string | null;
  tenant_identifier?: string | null;
  basis?: string | null;
  adapter_status?: string | null;
  adapter_key?: string | null;
  external_submission_enabled?: boolean;
};

function providerName(provider: string | null | undefined): string {
  if (!provider) return 'UNKNOWN';
  if (provider === 'greenhouse') return 'Greenhouse';
  if (provider === 'mock-ats') return 'Fictional mock ATS';
  return provider.split(/[-_]/).map(part => part.length
    ? part[0].toUpperCase() + part.slice(1) : part).join(' ');
}

function knowledgeBasis(basis: string | null | undefined): string {
  switch (basis) {
    case 'ROUTE_AND_DISCOVERY':
      return 'Selected route matches saved ATS discovery evidence';
    case 'COMPANY_DISCOVERY':
      return 'Company discovery';
    case 'ROUTE':
      return 'Saved application route';
    case 'AMBIGUOUS':
      return 'Conflicting company discovery evidence';
    case 'INSUFFICIENT_EVIDENCE':
      return 'Discovery evidence needs review';
    default:
      return 'UNKNOWN';
  }
}

function capabilityDescription(knowledge: ApplicationAdapterKnowledge): string {
  if (knowledge.adapter_status === 'FIXTURE_ONLY'
      && knowledge.adapter_key === 'mock-ats') {
    return 'Fictional local mock adapter is available in fixture mode only; fixture only.';
  }
  if (knowledge.adapter_status === 'UNSUPPORTED' && knowledge.provider) {
    return `No supported MJobs application adapter is available for ${providerName(knowledge.provider)}. Prepare documents for manual use.`;
  }
  if (knowledge.adapter_status === 'REVIEW_REQUIRED'
      || knowledge.basis === 'AMBIGUOUS'
      || knowledge.basis === 'INSUFFICIENT_EVIDENCE') {
    return 'This ATS identity needs review. MJobs did not choose a provider or adapter.';
  }
  return 'No supported application adapter is confirmed. Continue manually after reviewing the employer destination.';
}

function routeContext(knowledge: ApplicationAdapterKnowledge): string {
  switch (knowledge.basis) {
    case 'COMPANY_DISCOVERY':
      return 'Company discovery recorded this ATS, but the current application URL has not been verified as belonging to this tenant.';
    case 'ROUTE_AND_DISCOVERY':
      return 'The saved application route and persisted company ATS identity agree.';
    case 'ROUTE':
      return 'The provider comes from the saved application route; no matching company discovery record was selected.';
    case 'AMBIGUOUS':
      return 'More than one ATS identity is available; MJobs did not choose a provider or adapter.';
    case 'INSUFFICIENT_EVIDENCE':
      return 'The observed ATS evidence did not meet the threshold needed for a provider selection.';
    default:
      return 'No persisted ATS evidence is available for this application route.';
  }
}

export function ApplicationAdapterKnowledgePanel({
  knowledge = {},
}: {knowledge?: ApplicationAdapterKnowledge | null}) {
  const value = knowledge || {};
  return <section className="panel" aria-label="ATS application adapter knowledge">
    <h4>Known ATS and application adapter</h4>
    <dl>
      <dt>Known provider</dt><dd>{providerName(value.provider)}</dd>
      {value.tenant_identifier && <><dt>Known tenant</dt><dd>{value.tenant_identifier}</dd></>}
      <dt>Evidence basis</dt><dd>{knowledgeBasis(value.basis)}</dd>
      <dt>Application capability</dt><dd>{capabilityDescription(value)}</dd>
    </dl>
    <p>{routeContext(value)}</p>
    <p className="muted">This view reuses saved discovery and route records; it does not rediscover the employer or contact an employer. External application submission remains OFF.</p>
  </section>;
}
