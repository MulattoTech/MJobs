import React, { useState } from 'react';

export type ApplicationRouteDiscoveryResult = {
  source_route_id: string;
  status: 'RESOLVED_ATS_ROUTE' | 'RESOLVED_EMPLOYER_ROUTE' | 'UNRESOLVED' | 'BLOCKED';
  candidate_url?: string;
  provider?: string;
  tenant?: string;
};

type Props = {
  routeId: string;
  routeLabel: string;
  disabled?: boolean;
  onCheck: (routeId: string) => Promise<ApplicationRouteDiscoveryResult>;
};

export function safeCandidateUrl(value?: string) {
  if (!value) return null;
  try {
    const url = new URL(value);
    if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash) return null;
    return url.toString();
  } catch {
    return null;
  }
}

export function ApplicationRouteDiscovery({ routeId, routeLabel, disabled = false, onCheck }: Props) {
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<ApplicationRouteDiscoveryResult | null>(null);
  const [failed, setFailed] = useState(false);

  async function check() {
    setLoading(true);
    setFailed(false);
    setResult(null);
    try {
      const response = await onCheck(routeId);
      if (response.source_route_id !== routeId) {
        setFailed(true);
        return;
      }
      setResult(response);
    } catch {
      setFailed(true);
    } finally {
      setLoading(false);
    }
  }

  const candidate = safeCandidateUrl(result?.candidate_url);
  return <section className="fact" aria-label={`Employer route check for ${routeLabel}`}>
    <div className="toolbar"><div><strong>{routeLabel}</strong><p className="muted">Check only when requested. MJobs follows a short, public HTTPS redirect chain and never changes the selected route or submits.</p></div>
      <button type="button" disabled={disabled || loading} onClick={() => void check()}>{loading ? 'Checking employer route…' : 'Check employer route'}</button>
    </div>
    {failed && <p role="status">This route could not be checked. Your saved route and selection are unchanged.</p>}
    {result?.status === 'RESOLVED_ATS_ROUTE' && candidate && <div role="status">
      <p>Redirect reached the employer’s verified {result.provider || 'ATS'} tenant{result.tenant ? ` “${result.tenant}”` : ''}. Review the destination before using it. Your saved route and selection are unchanged.</p>
      <a href={candidate} target="_blank" rel="noreferrer">Review detected ATS destination ↗</a>
    </div>}
    {result?.status === 'RESOLVED_EMPLOYER_ROUTE' && candidate && <div role="status">
      <p>Redirect reached a verified employer domain. Review the destination before using it. Your saved route and selection are unchanged.</p>
      <a href={candidate} target="_blank" rel="noreferrer">Review detected employer destination ↗</a>
    </div>}
    {result && (result.status === 'UNRESOLVED' || result.status === 'BLOCKED' ||
      ((result.status === 'RESOLVED_ATS_ROUTE' || result.status === 'RESOLVED_EMPLOYER_ROUTE') && !candidate)) &&
      <p role="status">No verified destination was returned. Your saved route and selection are unchanged.</p>}
  </section>;
}
